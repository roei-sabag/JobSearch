"""
services/tailor_service.py
---------------------------
Phase 3: DB-backed / backend-triggered CV tailoring service.

Reuses (does not duplicate) the pipeline functions already implemented and
QA-validated in tailor_skills.py:
    - tailor_with_anthropic() / tailor_with_fallback()
    - validate_authenticity()
    - select_relevant_courses() / validate_courses_authenticity()
    - render_pdf() (parameterized with output path overrides)

This module wires that pipeline to the Job/CVMaster/CVTailored ORM models so
it can be triggered asynchronously (via FastAPI BackgroundTasks) per job_id,
persisting a full audit trail row per tailoring run.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path


from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import tailor_skills as ts
from db.models import CVMaster, CVTailored, Job
from services.notification_service import send_tailoring_notification




logger = logging.getLogger("tailor_service")

WORKDIR = Path(__file__).resolve().parent.parent
OUTPUT_CVS_DIR = WORKDIR / "output" / "cvs"
CV_MASTER_VERSION_LABEL = "v1-frozen-layout"

# Base filename (no number) always reserved for the MOST RECENTLY generated
# CV, per explicit instruction: "RoeiSabag_CV.pdf" should always be the most
# relevant/latest submission, with every older version shifted to
# "RoeiSabag_CV2.pdf", "RoeiSabag_CV3.pdf", etc.
CV_BASE_FILENAME = "RoeiSabag_CV"
_CV_NUMBERED_RE = re.compile(rf"^{re.escape(CV_BASE_FILENAME)}(\d*)\.pdf$")


async def _renumber_existing_cvs_and_get_new_path(session: AsyncSession) -> Path:
    """
    Shifts every existing CVTailored PDF's number up by one slot (both the
    actual file on disk AND the pdf_path column in the DB, so previously
    -shared download links/emails keep pointing at a valid file), freeing up
    the un-numbered "RoeiSabag_CV.pdf" slot for the brand-new PDF that's
    about to be rendered by the caller.

    Renumbering scheme (per explicit instruction):
      RoeiSabag_CV.pdf   (most recent so far) -> RoeiSabag_CV2.pdf
      RoeiSabag_CV2.pdf  -> RoeiSabag_CV3.pdf
      RoeiSabag_CV3.pdf  -> RoeiSabag_CV4.pdf
      ... and so on for every existing CVTailored row, ordered by
      created_at descending (most recent first) so "RoeiSabag_CV.pdf"
      always corresponds to whichever CVTailored row is truly the latest.

    Renumbering must always proceed from OLDEST -> NEWEST insertion order
    when actually touching the filesystem (i.e. shift the highest-numbered
    file first) to avoid a rename momentarily colliding with/overwriting a
    file that still needs to be moved itself. We compute every row's target
    filename first, then apply the renames in descending-number order.

    Called from BOTH tailor_cv_for_job() (initial tailoring) and
    finalize_courses_for_job() (course/soft-skill finalization + email
    send), since either one can produce a brand-new "latest" CV version.
    """
    result = await session.execute(
        select(CVTailored).order_by(CVTailored.created_at.desc())
    )
    rows = result.scalars().all()

    # Build the ordered list of (row, new_filename) pairs: row[0] (most
    # recent existing) -> CV2, row[1] -> CV3, etc. Rows whose pdf_path
    # doesn't match the expected naming scheme (e.g. legacy/manual files)
    # are left untouched entirely, to avoid accidentally clobbering
    # unrelated files.
    renumber_plan = []
    for i, row in enumerate(rows):
        old_path = Path(row.pdf_path) if row.pdf_path else None
        if old_path is None or not _CV_NUMBERED_RE.match(old_path.name):
            continue
        new_number = i + 2  # most recent existing row -> 2, next -> 3, ...
        new_filename = f"{CV_BASE_FILENAME}{new_number}.pdf"
        renumber_plan.append((row, old_path, new_filename))

    # Apply renames in DESCENDING target-number order (i.e. shift the
    # highest-numbered / oldest file first) so a rename never overwrites a
    # file that still needs to be moved itself.
    for row, old_path, new_filename in sorted(renumber_plan, key=lambda t: t[2], reverse=True):
        new_path = old_path.parent / new_filename
        try:
            if old_path.exists():
                old_path.replace(new_path)  # atomic overwrite-safe move
            row.pdf_path = str(new_path)

            # Keep the sibling rendered-HTML debug artifact in sync too, if
            # it exists, purely for on-disk tidiness (not referenced by any
            # DB column, so a missing/failed rename here is non-fatal).
            old_html = old_path.parent / f"_rendered_{old_path.stem}.html"
        except Exception:
            logger.exception(
                "[tailor_service] Failed to renumber existing CV file %s -> %s; "
                "leaving its pdf_path/file as-is.", old_path, new_filename,
            )

    return OUTPUT_CVS_DIR / f"{CV_BASE_FILENAME}.pdf"



async def ensure_cv_master_seeded(session: AsyncSession) -> CVMaster:
    """
    Get-or-create the single active CVMaster row, pointing at the frozen
    cv_template.html, with raw_text populated from all_my_skills.txt for
    audit/context purposes (ground-truth reference, never mutated at
    tailoring time).
    """
    result = await session.execute(
        select(CVMaster).where(CVMaster.version_label == CV_MASTER_VERSION_LABEL)
    )
    cv_master = result.scalar_one_or_none()
    if cv_master is not None:
        return cv_master

    raw_text = ""
    skills_txt_path = WORKDIR / "all_my_skills.txt"
    if skills_txt_path.exists():
        raw_text = skills_txt_path.read_text(encoding="utf-8")

    cv_master = CVMaster(
        version_label=CV_MASTER_VERSION_LABEL,
        html_template_path=str(ts.TEMPLATE_PATH),
        raw_text=raw_text,
    )
    session.add(cv_master)
    await session.flush()  # populate cv_master.id without committing yet
    return cv_master


async def tailor_cv_for_job(job_id: int, session: AsyncSession) -> CVTailored:
    """
    Full tailoring pipeline for a single Job, backed by the DB:
      1. Fetch the Job (raise if missing).
      2. Fetch/seed the active CVMaster.
      3. Run the Anthropic-or-fallback tailoring pipeline against the job's
         raw_description as the JD text.
      4. Apply the authenticity guardrail (strip any fabricated skills).
      5. Select Relevant Coursework deterministically + apply its guardrail.
      6. Render via Playwright to output/cvs/tailored_job_{job_id}.pdf.
      7. Insert a CVTailored audit row with the full JSON dump of everything
         used/decided.
      8. Update job.status = "tailored", commit, return the record.

    On any exception: sets job.status = "tailor_failed", logs the error, and
    re-raises so the background-task wrapper can also log it -- this
    function must never fail silently.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    try:
        cv_master = await ensure_cv_master_seeded(session)

        jd_text = job.raw_description
        pool = ts.load_json(ts.SKILLS_POOL_PATH)
        allowed_skills = ts.flatten_pool(pool)
        courses_pool = ts.load_json(ts.COURSES_POOL_PATH)

        # NOTE (multi-agent consensus upgrade): when at least Anthropic +
        # OpenAI keys are both configured, use the ensemble pipeline
        # (tailor_with_multi_agent_consensus) - Claude + GPT-4o tailor the
        # JD independently in parallel, and Gemini (if GOOGLE_API_KEY is
        # also set) arbitrates between them for a single reconciled result.
        # This runs SYNCHRONOUSLY-BLOCKING SDK calls internally via worker
        # threads with hard timeouts, so it's offloaded to a thread here via
        # asyncio.to_thread to avoid blocking the FastAPI event loop.
        # Falls back to the single-Anthropic-agent path (and finally the
        # deterministic local ranker) exactly as before if the ensemble
        # can't be used or fails entirely - see tailor_with_multi_agent_consensus()'s
        # docstring for the full fallback hierarchy.
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")

        try:
            if anthropic_key and openai_key:
                response, mode = await asyncio.to_thread(
                    ts.tailor_with_multi_agent_consensus, jd_text, pool, courses_pool
                )
            elif anthropic_key:
                response = await asyncio.to_thread(ts.tailor_with_anthropic, jd_text, pool, courses_pool)
                mode = "anthropic-claude-sonnet-4-5 (single-agent, openai key not configured)"
            else:
                response = ts.tailor_with_fallback(jd_text, pool)
                mode = "local-fallback-keyword-overlap"
        except Exception as e:
            # NOTE (bug fix, diagnosability): previously only the exception
            # message was logged here, which for a Pydantic ValidationError
            # (e.g. the "excel"/ambiguous-term guardrail bug that silently
            # forced EVERY run for a given JD onto the local fallback path)
            # only showed the validator's error text, not what raw LLM
            # response actually triggered it. logger.exception() below
            # includes the full traceback, and str(e) for pydantic
            # ValidationErrors already includes the offending field/value,
            # but we also explicitly log at WARNING (not just visible with
            # DEBUG level) so this is never silently missed in production
            # logs again.
            logger.exception(
                "[tailor_service] LLM path(s) failed for job_id=%s (%s); falling back to local ranker. "
                "If this is a Pydantic ValidationError, check the message above for which "
                "guardrail/field rejected the LLM's response.",
                job_id, e,
            )
            mode = "local-fallback-keyword-overlap (after LLM error)"
            response = ts.tailor_with_fallback(jd_text, pool)


        violations = ts.validate_authenticity(response, allowed_skills)
        if violations:
            logger.warning("[tailor_service] Authenticity guardrail violations, stripping: %s", violations)
            allowed_set = {s.strip().lower() for s in allowed_skills}
            for cat in response.categories:
                cat.skills = [s for s in cat.skills if s.strip().lower() in allowed_set]

        relevant_courses = ts.select_relevant_courses(jd_text, courses_pool)
        course_violations = ts.validate_courses_authenticity(relevant_courses, courses_pool)
        if course_violations:
            logger.warning("[tailor_service] Course authenticity violations, dropping: %s", course_violations)
            ground_truth = {c["name"]: c["grade"] for c in courses_pool.get("courses", [])}
            relevant_courses = [
                c for c in relevant_courses
                if c["name"] in ground_truth and ground_truth[c["name"]] == c["grade"]
                and ts.MIN_COURSE_GRADE <= c["grade"] <= ts.MAX_COURSE_GRADE
            ]

        # Dedicated, unconstrained job-analysis LLM call (separate from the
        # CV-tailoring call above) for the human-facing notification email --
        # see analyze_job_posting()'s docstring for why this is intentionally
        # a second API call rather than reusing/expanding the CV-tailoring
        # response. Only run once per job (here, at initial tailoring time);
        # finalize_courses_for_job() below reuses this same stored result
        # rather than re-calling the LLM.
        skills_txt_path = WORKDIR / "all_my_skills.txt"
        candidate_background = skills_txt_path.read_text(encoding="utf-8") if skills_txt_path.exists() else ""
        job_analysis = await asyncio.to_thread(ts.analyze_job_posting, jd_text, candidate_background)

        # NOTE (semantic relevance scores feature): persist the 4
        # LLM-judged 0-100 relevance-score arrays (skill_scores,
        # soft_skill_scores, domain_scores, course_scores) produced by
        # the LLM-based tailoring paths (tailor_with_anthropic() /
        # tailor_with_openai() / _reconcile_with_gemini(), via
        # _build_tailoring_prompts()'s shared instructions), so the
        # human-in-the-loop pickers below can prefer genuine semantic
        # relevance over their existing deterministic keyword-overlap
        # heuristics. These default to empty lists for
        # tailor_with_fallback() (the local, non-LLM ranker), so old/
        # fallback-mode jobs simply fall back to the existing
        # deterministic logic unchanged - see each
        # get_*_options_with_suggestions() function's docstring in
        # tailor_skills.py.
        tailored_fields = {
            "mode": mode,
            "categories": [c.model_dump() for c in response.categories],
            "soft_skills_line": response.soft_skills_line,
            "seeking_line": response.seeking_line,
            "rationale": response.rationale,

            "omitted": response.omitted,
            "relevant_courses": relevant_courses,
            "authenticity_violations": violations,
            "course_authenticity_violations": course_violations,
            "job_analysis": job_analysis,
            "skill_scores": [s.model_dump() for s in response.skill_scores],
            "soft_skill_scores": [s.model_dump() for s in response.soft_skill_scores],
            "domain_scores": [s.model_dump() for s in response.domain_scores],
            "course_scores": [s.model_dump() for s in response.course_scores],
        }


        # NOTE (naming scheme, per explicit instruction): the MOST RECENT CV
        # is always named "RoeiSabag_CV.pdf" (no number), so the freshest
        # submission is unambiguously the most relevant file to send.
        # BEFORE creating this new row, every existing CVTailored PDF is
        # shifted up by one number slot (RoeiSabag_CV.pdf -> CV2.pdf,
        # CV2.pdf -> CV3.pdf, etc.) via _renumber_existing_cvs_and_get_new_path(),
        # which also frees up the un-numbered filename for this new render.
        cv_tailored = CVTailored(
            job_id=job.id,
            cv_master_id=cv_master.id,
            tailored_fields_json=json.dumps(tailored_fields, indent=2),
            pdf_path="",  # placeholder, set below once we know the row's id
        )
        session.add(cv_tailored)
        await session.flush()  # populates cv_tailored.id without committing

        OUTPUT_CVS_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = await _renumber_existing_cvs_and_get_new_path(session)
        rendered_html_path = OUTPUT_CVS_DIR / f"_rendered_job_{job_id}_v{cv_tailored.id}.html"


        # render_pdf() uses Playwright's SYNC API internally, which cannot
        # run inside an already-active asyncio event loop (FastAPI/uvicorn's
        # loop). Offload it to a worker thread via asyncio.to_thread so the
        # sync Playwright call gets its own thread with no running loop.
        title_line = ts.build_title_line()

        await asyncio.to_thread(
            ts.render_pdf,
            response,
            relevant_courses,
            output_pdf_path=pdf_path,
            rendered_html_path=rendered_html_path,
            template_path=ts.TEMPLATE_PATH,
            title_line=title_line,
        )

        cv_tailored.pdf_path = str(pdf_path)
        tailored_fields["title_line"] = title_line
        cv_tailored.tailored_fields_json = json.dumps(tailored_fields, indent=2)

        job.status = "tailored"


        await session.commit()
        await session.refresh(cv_tailored)


        # NOTE (bug fix): the notification email is intentionally NOT sent
        # here anymore. Previously this fired immediately after the FIRST
        # (default) render -- before the user ever got a chance to review
        # and finalize the "Relevant Coursework" selection in the UI course
        # picker. That meant the emailed PDF could be stale (default/algorithm
        # -suggested courses only) rather than the user's actual final pick.
        # The email is now sent from finalize_courses_for_job() /
        # api/routers/jobs.py's /finalize-courses endpoint instead, which is
        # the true "user is done, send it" checkpoint in the UI flow -- see
        # that function's docstring for the full rationale.
        return cv_tailored


    except Exception:
        logger.exception("[tailor_service] Tailoring failed for job_id=%s", job_id)

        await session.rollback()
        # Re-fetch job in a fresh transaction context to safely set failure status
        job = await session.get(Job, job_id)
        if job is not None:
            job.status = "tailor_failed"
            await session.commit()
        raise


def _get_latest_semantic_scores(job_id: int, session: AsyncSession) -> dict:
    """placeholder - not used; real implementation below is async."""
    raise NotImplementedError


async def _get_latest_tailored_fields(job_id: int, session: AsyncSession) -> dict:
    """
    Fetches the most recent CVTailored row for a job and returns its parsed
    tailored_fields dict, or an empty dict if no tailoring run exists yet.
    Shared helper for the get_*_options_for_job() wrapper functions below so
    they can pass the stored semantic relevance-score arrays (skill_scores/
    soft_skill_scores/domain_scores/course_scores) through to the picker
    functions in tailor_skills.py, which prefer these genuine LLM-judged
    scores over their deterministic keyword-overlap fallback logic when
    available (old jobs / fallback-mode runs simply have empty score lists
    here, so the pickers seamlessly fall back to their existing behavior).
    """
    result = await session.execute(
        select(CVTailored)
        .where(CVTailored.job_id == job_id)
        .order_by(CVTailored.created_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        return {}
    return json.loads(latest.tailored_fields_json)


async def get_course_options_for_job(job_id: int, session: AsyncSession) -> list[dict]:
    """
    Returns every eligible ground-truth course (grade 80-100) annotated with
    a "suggested" flag reflecting what the deterministic algorithm would
    have picked for THIS job's JD text - powers the human-in-the-loop course
    picker in the UI. Prefers the stored "course_scores" semantic relevance
    scores from the most recent tailoring run, if available, falling back to
    the deterministic keyword-tag heuristic for old/fallback-mode jobs.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    courses_pool = ts.load_json(ts.COURSES_POOL_PATH)
    tailored_fields = await _get_latest_tailored_fields(job_id, session)
    semantic_scores = tailored_fields.get("course_scores")
    return ts.get_course_options_with_suggestions(job.raw_description, courses_pool, semantic_scores=semantic_scores)


async def get_soft_skill_options_for_job(job_id: int, session: AsyncSession) -> list[dict]:
    """
    Returns every entry in the growable soft-skill bank, annotated with a
    "suggested" flag reflecting what the deterministic ranker would have
    picked for THIS job's JD text, plus a match_percentage - powers the
    human-in-the-loop "Relevant Soft Skills" picker in the UI (mirrors
    get_course_options_for_job()). Prefers the stored "soft_skill_scores"
    semantic relevance scores from the most recent tailoring run, if
    available, falling back to the deterministic keyword-overlap heuristic
    for old/fallback-mode jobs.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    tailored_fields = await _get_latest_tailored_fields(job_id, session)
    semantic_scores = tailored_fields.get("soft_skill_scores")
    return ts.get_soft_skill_options_with_suggestions(job.raw_description, semantic_scores=semantic_scores)


async def get_domain_options_for_job(job_id: int, session: AsyncSession) -> dict:
    """
    Returns every entry in the authentic seeking-domains bank, annotated with
    a "suggested" flag reflecting what the deterministic ranker would have
    picked for THIS job's JD text, plus a match_percentage and a
    "suggested_role" phrase - powers the human-in-the-loop "Target Role &
    Domains" picker in the UI (mirrors get_course_options_for_job() /
    get_soft_skill_options_for_job()). Prefers the stored "domain_scores"
    semantic relevance scores from the most recent tailoring run, if
    available, falling back to the deterministic keyword-overlap heuristic
    for old/fallback-mode jobs.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    tailored_fields = await _get_latest_tailored_fields(job_id, session)
    semantic_scores = tailored_fields.get("domain_scores")
    return ts.get_domain_options_with_suggestions(job.raw_description, job_title=job.title, semantic_scores=semantic_scores)


async def get_skill_options_for_job(job_id: int, session: AsyncSession) -> list[dict]:
    """
    Returns every skill in the ground-truth skills_pool.json, grouped by
    category, each annotated with a "suggested" flag reflecting whether the
    most recent tailoring run for this job actually included it, plus a
    match_percentage - powers the human-in-the-loop "Skills" picker in the
    UI (mirrors get_course_options_for_job() / get_soft_skill_options_for_job()).
    Prefers the stored "skill_scores" semantic relevance scores from the
    most recent tailoring run, if available, falling back to the
    deterministic keyword-overlap heuristic (and the previously-selected
    categories) for old/fallback-mode jobs.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    pool = ts.load_json(ts.SKILLS_POOL_PATH)

    # Prefer the actual categories chosen by the most recent tailoring run
    # (the TRUE algorithm suggestion), falling back to a fresh keyword
    # heuristic if no previous run exists yet.
    previous_fields = await _get_latest_tailored_fields(job_id, session)
    previously_selected = None
    if previous_fields:
        previously_selected = {
            c["name"]: c.get("skills", []) for c in previous_fields.get("categories", [])
        }
    semantic_scores = previous_fields.get("skill_scores")

    return ts.get_skill_options_with_suggestions(
        job.raw_description, pool, previously_selected=previously_selected, semantic_scores=semantic_scores
    )


async def finalize_courses_for_job(
    job_id: int,
    course_names: list[str],
    session: AsyncSession,
    soft_skill_phrases: list[str] | None = None,
    role_phrase: str | None = None,
    selected_domains: list[str] | None = None,
    role_qualifier: str | None = None,
    semesters_remaining: int | None = None,
    include_student_in_title: bool | None = None,
    selected_skills: list[str] | None = None,
) -> tuple[CVTailored, bool]:



    """
    Re-renders the CV PDF for a job using its MOST RECENT tailored skills
    selection (categories/seeking_line are left untouched -
    this endpoint lets the user override which Relevant Coursework entries
    appear AND which soft-skill phrases appear in soft_skills_line), based
    on a user-approved list of course names + soft-skill phrases from the
    human-in-the-loop pickers. Every course name is re-validated against the
    ground-truth pool (name + grade must match exactly, and grade must be in
    the allowed 80-100 range) before being used - the authenticity guardrail
    is never bypassed just because a human clicked it. soft_skill_phrases
    are joined with the shared join_with_and() helper (comma-separated,
    "and" before the final phrase, no Oxford comma, never "or") to rebuild
    soft_skills_line exactly per the mandated format. Inserts a new
    CVTailored audit row (does not mutate the previous one).
    """
    soft_skill_phrases = soft_skill_phrases or []

    job = await session.get(Job, job_id)

    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    # Fetch the most recent tailoring run for this job to reuse its already
    # LLM/fallback-selected skills + dynamic summary fields unchanged.
    result = await session.execute(
        select(CVTailored)
        .where(CVTailored.job_id == job_id)
        .order_by(CVTailored.created_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        raise ValueError(f"No existing tailored CV found for job_id={job_id}; run /tailor first.")

    previous_fields = json.loads(latest.tailored_fields_json)

    courses_pool = ts.load_json(ts.COURSES_POOL_PATH)
    ground_truth = {c["name"]: c for c in courses_pool.get("courses", [])}

    # Re-derive the full course objects (name/grade/tags) strictly from the
    # ground-truth pool for each user-selected name - never trust arbitrary
    # client-supplied grade/tag data, only the name selection itself.
    selected_courses = []
    for name in course_names:
        if name in ground_truth:
            gt = ground_truth[name]
            selected_courses.append({
                "name": gt["name"],
                "grade": gt["grade"],
                "tags": gt.get("tags", []),
            })

    course_violations = ts.validate_courses_authenticity(selected_courses, courses_pool)
    if course_violations:
        logger.warning("[tailor_service] Course authenticity violations on finalize, dropping: %s", course_violations)
        selected_courses = [
            c for c in selected_courses
            if c["name"] in ground_truth and ground_truth[c["name"]]["grade"] == c["grade"]
            and ts.MIN_COURSE_GRADE <= c["grade"] <= ts.MAX_COURSE_GRADE
        ]

    # Rebuild soft_skills_line from the user's manually-approved phrase
    # selection in the human-in-the-loop Soft Skills picker, if provided.
    # Every phrase is re-validated against the growable SOFT_SKILL_BANK
    # (never trust arbitrary client-supplied phrase text) before being
    # joined with the shared join_with_and() helper (comma-separated, "and"
    # before the final phrase, no Oxford comma, never "or") - exactly the
    # mandated soft_skills_line format. If no phrases were supplied, the
    # previous run's soft_skills_line is left completely unchanged.
    soft_skills_line = previous_fields["soft_skills_line"]
    mode_suffix = " (courses manually finalized by user)"
    if soft_skill_phrases:
        bank_phrases = {entry["phrase"] for entry in ts.SOFT_SKILL_BANK}
        valid_phrases = [p for p in soft_skill_phrases if p in bank_phrases]
        if valid_phrases:
            soft_skills_line = ts.join_with_and(valid_phrases)
            mode_suffix = " (courses + soft skills manually finalized by user)"

    # Rebuild seeking_line from the user's manually-approved role phrase +
    # domain selection in the human-in-the-loop "Target Role & Domains"
    # picker, if provided. Every domain is re-validated against the
    # authentic seeking-domains bank (never trust arbitrary client-supplied
    # domain text) via validate_domains_authenticity() before being joined
    # with build_seeking_line() (which itself uses the shared
    # join_with_and() helper) - exactly the mandated seeking_line format. If
    # no domains were supplied, the previous run's seeking_line is left
    # completely unchanged (role_phrase alone, without domains, is not
    # enough to rebuild a valid sentence).
    seeking_line = previous_fields["seeking_line"]
    selected_domains = selected_domains or []
    if selected_domains:
        domain_violations = ts.validate_domains_authenticity(selected_domains)
        # Filter out only the domains that actually triggered a violation,
        # rather than discarding the whole selection on a single bad entry.
        if domain_violations:

            logger.warning(
                "[tailor_service] Domain authenticity violations on finalize, dropping invalid entries: %s",
                domain_violations,
            )
            authentic_bank = ts.AUTHENTIC_SEEKING_DOMAINS
            valid_domains = [
                d for d in selected_domains
                if any(
                    d.lower() == x.lower() or d.lower() in x.lower() or x.lower() in d.lower()
                    for x in authentic_bank
                )
            ]
        else:
            valid_domains = selected_domains

        if valid_domains:
            effective_role = (role_phrase or "").strip()
            if not effective_role:
                # Fall back to extracting the previous role phrase from the
                # existing seeking_line (text before " Student position").
                match = re.search(r"Seeking an? (.+?) (?:Student|Junior) position", previous_fields["seeking_line"])
                effective_role = match.group(1) if match else "Engineering"
            effective_qualifier = role_qualifier if role_qualifier in ts.ALLOWED_ROLE_QUALIFIERS else ts.DEFAULT_ROLE_QUALIFIER
            seeking_line = ts.build_seeking_line(effective_role, valid_domains, role_qualifier=effective_qualifier)
            mode_suffix += " + role/domains manually finalized by user"
    elif role_qualifier in ts.ALLOWED_ROLE_QUALIFIERS:
        # No domain change, but the user still wants to swap Student<->Junior
        # on the existing seeking_line - do a minimal in-place word swap
        # rather than requiring a full domain resubmission.
        for other in ts.ALLOWED_ROLE_QUALIFIERS:
            if other != role_qualifier and f" {other} position" in seeking_line:
                seeking_line = seeking_line.replace(f" {other} position", f" {role_qualifier} position")
                mode_suffix += " + role qualifier manually finalized by user"
                break

    # Rebuild the header's title_line from the user's manually-approved
    # semesters-remaining + include-Student selections, if provided. Falls
    # back to the previous run's stored title_line (or the module default)
    # when neither override is supplied.
    previous_title_line = previous_fields.get("title_line")
    if semesters_remaining is not None or include_student_in_title is not None:
        effective_semesters = semesters_remaining if semesters_remaining is not None else ts.DEFAULT_SEMESTERS_REMAINING
        effective_include_student = include_student_in_title if include_student_in_title is not None else ts.DEFAULT_INCLUDE_STUDENT_IN_TITLE
        title_line = ts.build_title_line(
            semesters_remaining=effective_semesters,
            include_student=effective_include_student,
        )
        mode_suffix += " + title line manually finalized by user"
    else:
        title_line = previous_title_line or ts.build_title_line()

    # Rebuild the Skills section categories from the user's manually-approved
    # skill selection in the new human-in-the-loop Skills picker, if
    # provided. Every skill is re-validated against the ground-truth
    # skills_pool.json (never trust arbitrary client-supplied skill text)
    # before being kept - the authenticity guardrail can never be bypassed
    # just because a human clicked it.
    #
    # IMPORTANT: we iterate over the FULL ground-truth skills_pool.json
    # categories here (not just previous_fields["categories"], which only
    # contains the subset of categories the initial LLM tailoring pass
    # happened to pick, capped at MAX_CATEGORIES). Iterating only over the
    # previous subset meant any skill the user picked from a category that
    # wasn't part of the original selection (e.g. "AI & Automation" when the
    # LLM only chose "Hardware & Verification" + "Programming & Tools")
    # would be silently dropped on the floor - the user's explicit skill
    # picks must always be able to introduce a brand-new category into the
    # final CV. Category ORDER still follows skills_pool.json's own order
    # (its natural/curated ordering), and only categories with at least one
    # user-selected skill are kept in the final output.
    categories_for_response = previous_fields["categories"]
    skill_violations: list[str] = []
    if selected_skills is not None:
        skills_pool = ts.load_json(ts.SKILLS_POOL_PATH)
        selected_set = {s.lower() for s in selected_skills}
        candidate_categories = []
        selected_by_category: dict[str, list[str]] = {}
        for cat in skills_pool.get("categories", []):
            kept = [s for s in cat.get("skills", []) if s.lower() in selected_set]
            if kept:
                candidate_categories.append({"name": cat["name"], "skills": kept})
                selected_by_category[cat["name"]] = kept


        skill_violations = ts.validate_skill_selection_authenticity(selected_by_category, skills_pool)
        if skill_violations:
            logger.warning("[tailor_service] Skill authenticity violations on finalize, dropping: %s", skill_violations)
            allowed_set = {
                s.lower() for cat in skills_pool.get("categories", []) for s in cat.get("skills", [])
            }
            candidate_categories = [
                {"name": cat["name"], "skills": [s for s in cat["skills"] if s.lower() in allowed_set]}
                for cat in candidate_categories
            ]

        # Only apply the filtered categories if at least one category still
        # has a skill left - never render a CV with a completely empty
        # Skills section just because the user's selection didn't map onto
        # any previously-tailored category.
        if any(cat["skills"] for cat in candidate_categories):
            categories_for_response = candidate_categories
            mode_suffix += " + skills manually finalized by user"

    # Reconstruct the TailoredSkillsResponse from the previous run's stored
    # fields so render_pdf() gets its expected object shape, without calling
    # the LLM again (hard skills are intentionally left unchanged unless the
    # user picked specific skills via the new Skills picker - only
    # soft_skills_line, seeking_line, relevant_courses, title_line and now
    # skills can be overridden by the human-in-the-loop pickers).
    response = ts.TailoredSkillsResponse(
        categories=[ts.SkillCategory(**c) for c in categories_for_response],
        soft_skills_line=soft_skills_line,
        seeking_line=seeking_line,
        rationale=previous_fields.get("rationale", []),
        omitted=previous_fields.get("omitted", []),
    )



    tailored_fields = {
        **previous_fields,
        "mode": previous_fields.get("mode", "unknown") + mode_suffix,
        "soft_skills_line": soft_skills_line,
        "seeking_line": seeking_line,
        "title_line": title_line,
        "categories": [c.model_dump() for c in response.categories],
        "relevant_courses": selected_courses,
        "course_authenticity_violations": course_violations,
        "skill_authenticity_violations": skill_violations,
    }



    # NOTE (naming scheme, same reasoning as tailor_cv_for_job): the MOST
    # RECENT CV is always named "RoeiSabag_CV.pdf" (no number). Every
    # existing CVTailored PDF is shifted up by one number slot via
    # _renumber_existing_cvs_and_get_new_path() BEFORE this new row is
    # rendered, freeing up the un-numbered filename for this new render --
    # applies here too since finalize-courses can also produce a brand-new
    # "latest" CV version (this is the true "send the email" checkpoint).
    cv_tailored = CVTailored(
        job_id=job.id,
        cv_master_id=latest.cv_master_id,
        tailored_fields_json=json.dumps(tailored_fields, indent=2),
        pdf_path="",  # placeholder, set below once we know the row's id
    )
    session.add(cv_tailored)
    await session.flush()  # populates cv_tailored.id without committing

    OUTPUT_CVS_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = await _renumber_existing_cvs_and_get_new_path(session)
    rendered_html_path = OUTPUT_CVS_DIR / f"_rendered_job_{job_id}_v{cv_tailored.id}.html"


    await asyncio.to_thread(
        ts.render_pdf,
        response,
        selected_courses,
        output_pdf_path=pdf_path,
        rendered_html_path=rendered_html_path,
        template_path=ts.TEMPLATE_PATH,
        title_line=title_line,
    )


    cv_tailored.pdf_path = str(pdf_path)

    await session.commit()
    await session.refresh(cv_tailored)


    # Bug fix: this is the TRUE "user has finished reviewing/approving the
    # Relevant Coursework selection" checkpoint in the UI flow (the course
    # picker panel's "Update Coursework & Re-render" button), so the
    # notification email is sent HERE now instead of immediately after the
    # first/default tailoring render in tailor_cv_for_job(). This guarantees
    # the emailed PDF always reflects the user's final course choices, never
    # a stale default. Isolated in its own try/except, exactly like the
    # previous location, so a failed email can never affect the
    # already-committed, successfully re-rendered CV result above.
    email_sent = False
    try:
        email_sent = await send_tailoring_notification(job.id, cv_tailored.id, session)
    except Exception:
        logger.exception(
            "[tailor_service] Notification email failed for job_id=%s after "
            "course finalization; tailoring result is unaffected.",
            job_id,
        )

    return cv_tailored, email_sent
