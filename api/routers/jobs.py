"""
api/routers/jobs.py
--------------------
Manual job-ingestion endpoint (Module B: Manual Ingestion Port), plus small
read endpoints useful for verification and for later phases (scraper
insertion, tailoring pipeline trigger, etc.).
"""

import json
import logging
from typing import List, Optional


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.schemas import (
    CourseOptionsResponse,
    CVTailoredOut,
    DomainOptionsResponse,
    FinalizeCoursesRequest,
    FinalizeCoursesResponse,
    JobIngestRawRequest,
    JobIngestRawResponse,
    JobIngestRequest,
    JobIngestResponse,
    JobOut,
    SkillOptionsResponse,
    SoftSkillOptionsResponse,
    TailorJobResponse,
)
from db.base import AsyncSessionLocal
from db.models import Company, CVTailored, Job, compute_content_hash
from services.ingestion_service import extract_job_fields_from_raw_text
from services.tailor_service import (
    finalize_courses_for_job,
    get_course_options_for_job,
    get_domain_options_for_job,
    get_skill_options_for_job,
    get_soft_skill_options_for_job,
    tailor_cv_for_job,
)






logger = logging.getLogger("jobs_router")


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def _get_or_create_company(session: AsyncSession, company_name: Optional[str]) -> Optional[Company]:
    """Case-insensitive get-or-create so callers never need to know a company_id."""
    if not company_name or not company_name.strip():
        return None
    name = company_name.strip()

    result = await session.execute(select(Company).where(Company.name.ilike(name)))
    company = result.scalar_one_or_none()
    if company is not None:
        return company

    company = Company(name=name)
    session.add(company)
    await session.flush()  # populate company.id without committing yet
    return company


class _JobRecordResult:
    """Small internal carrier so the shared helper can report both the Job
    row and whether it was newly created or a pre-existing duplicate."""

    def __init__(self, job: Job, company_name: Optional[str], is_duplicate: bool):
        self.job = job
        self.company_name = company_name
        self.is_duplicate = is_duplicate


async def _create_job_record(
    session: AsyncSession,
    title: str,
    company_name: Optional[str],
    raw_description: str,
    source: str,
    source_url: Optional[str] = None,
) -> _JobRecordResult:
    """
    Shared get-or-create + dedup-check + insert logic used by BOTH
    /ingest and /ingest-raw, so the two entry points never drift apart.
    """
    content_hash = compute_content_hash(title, company_name, raw_description)

    existing = await session.execute(select(Job).where(Job.content_hash == content_hash))
    existing_job = existing.scalar_one_or_none()
    if existing_job is not None:
        existing_company_name = None
        if existing_job.company_id is not None:
            comp = await session.get(Company, existing_job.company_id)
            existing_company_name = comp.name if comp else None
        return _JobRecordResult(existing_job, existing_company_name, is_duplicate=True)

    company = await _get_or_create_company(session, company_name)

    job = Job(
        title=title.strip(),
        company_id=company.id if company else None,
        raw_description=raw_description,
        source=source.strip(),
        source_url=source_url,
        content_hash=content_hash,
        status="scraped",
    )
    session.add(job)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise HTTPException(status_code=500, detail="Failed to save job due to a database error.")

    await session.refresh(job)

    return _JobRecordResult(job, company.name if company else None, is_duplicate=False)


@router.post("/ingest", response_model=JobIngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_job(payload: JobIngestRequest, session: AsyncSession = Depends(get_session)):
    """
    Manually ingest a raw job description (e.g. copy-pasted from a WhatsApp
    or Discord group). Computes a content_hash to deduplicate against
    already-known postings; if a duplicate is detected, returns the existing
    record (HTTP 200-equivalent info) rather than raising, since re-sending
    the same job text is an expected, harmless occurrence for this channel.
    """
    result = await _create_job_record(
        session,
        title=payload.title,
        company_name=payload.company_name,
        raw_description=payload.raw_description,
        source=payload.source,
        source_url=payload.source_url,
    )
    job = result.job

    return JobIngestResponse(
        job_id=job.id,
        title=job.title,
        company_name=result.company_name,
        source=job.source,
        status=job.status,
        content_hash=job.content_hash,
        is_duplicate=result.is_duplicate,
        created_at=job.created_at,
    )


@router.post("/ingest-raw", response_model=JobIngestRawResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_job_raw(
    payload: JobIngestRawRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    Intelligent single-input ingestion: accepts ONE raw, unformatted text blob
    (e.g. a messy copy-paste from a WhatsApp group or LinkedIn), uses an LLM
    (with a deterministic fallback -- never 500s due to LLM unavailability)
    to extract {title, company_name, cleaned_description}, then reuses the
    exact same get-or-create/dedup/insert logic as /ingest, and immediately
    queues the background tailoring task -- true one-click "paste and go".
    """
    try:
        extracted, extraction_mode = extract_job_fields_from_raw_text(payload.raw_text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    result = await _create_job_record(
        session,
        title=extracted.title,
        company_name=extracted.company_name,
        raw_description=extracted.cleaned_description,
        source="manual_raw_text",
        source_url=None,
    )
    job = result.job

    # NOTE (TEMPORARY, per explicit instruction): the duplicate-submission
    # guard is disabled for now, until the tailoring pipeline reaches full
    # accuracy/automation. Previously, re-submitting the exact same job text
    # (e.g. to re-test a pipeline fix like the seeking_line/soft-skills
    # scoring improvements) was silently blocked from re-running the
    # tailoring pipeline at all, since `is_duplicate=True` short-circuited
    # this call entirely. We STILL avoid creating a duplicate Job row in the
    # DB (that dedup logic in _create_job_record() is untouched and still
    # returns the existing Job), but we now ALWAYS queue a fresh tailoring
    # run against that existing job, producing a new CVTailored version each
    # time. TO RE-ENABLE the original duplicate-blocking behavior later,
    # simply restore the `if not result.is_duplicate:` guard below.
    background_tasks.add_task(_run_tailoring_background, job.id)


    return JobIngestRawResponse(
        job_id=job.id,
        title=job.title,
        company_name=result.company_name,
        status=job.status,
        content_hash=job.content_hash,
        is_duplicate=result.is_duplicate,
        extraction_mode=extraction_mode,
        created_at=job.created_at,
    )



async def _get_tailored_versions(session: AsyncSession, job_id: int) -> List[CVTailoredOut]:
    result = await session.execute(
        select(CVTailored).where(CVTailored.job_id == job_id).order_by(CVTailored.created_at.desc())
    )
    rows = result.scalars().all()
    return [
        CVTailoredOut(id=r.id, pdf_path=r.pdf_path, created_at=r.created_at)
        for r in rows
    ]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    company_name = None
    if job.company_id is not None:
        comp = await session.get(Company, job.company_id)
        company_name = comp.name if comp else None
    tailored_versions = await _get_tailored_versions(session, job_id)
    return JobOut(
        id=job.id,
        title=job.title,
        company_name=company_name,
        raw_description=job.raw_description,
        source=job.source,
        source_url=job.source_url,
        content_hash=job.content_hash,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        tailored_versions=tailored_versions,
    )



@router.get("", response_model=List[JobOut])
async def list_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
    result = await session.execute(stmt)
    jobs = result.scalars().all()

    out = []
    for job in jobs:
        company_name = None
        if job.company_id is not None:
            comp = await session.get(Company, job.company_id)
            company_name = comp.name if comp else None
        tailored_versions = await _get_tailored_versions(session, job.id)
        out.append(
            JobOut(
                id=job.id,
                title=job.title,
                company_name=company_name,
                raw_description=job.raw_description,
                source=job.source,
                source_url=job.source_url,
                content_hash=job.content_hash,
                status=job.status,
                created_at=job.created_at,
                updated_at=job.updated_at,
                tailored_versions=tailored_versions,
            )
        )
    return out


async def _run_tailoring_background(job_id: int):
    """
    Background-task wrapper that opens its OWN fresh AsyncSessionLocal()
    context rather than reusing the request-scoped session from Depends,
    to avoid session-lifecycle ("session closed") bugs -- this is a
    settled design decision (see project_state.md Phase 3 notes).
    """
    async with AsyncSessionLocal() as session:
        try:
            await tailor_cv_for_job(job_id, session)
        except Exception:
            logger.exception("[jobs_router] Background tailoring task failed for job_id=%s", job_id)


@router.post("/{job_id}/tailor", response_model=TailorJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def tailor_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    Triggers the CV tailoring pipeline for a job, asynchronously, via
    FastAPI BackgroundTasks. Returns immediately with a 202 while the actual
    tailoring (LLM/fallback selection, guardrails, Playwright PDF render,
    CVTailored audit row insert) runs in the background.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    background_tasks.add_task(_run_tailoring_background, job_id)

    return TailorJobResponse(
        status="processing",
        message="Resume tailoring has been queued in the background",
        job_id=job_id,
    )


@router.get("/{job_id}/course-options", response_model=CourseOptionsResponse)
async def get_job_course_options(job_id: int, session: AsyncSession = Depends(get_session)):
    """
    Human-in-the-loop course picker support: returns EVERY eligible course
    from the ground-truth transcript pool (grade 80-100), each flagged with
    whether the deterministic relevance algorithm suggested it for this
    specific job's JD text. The frontend uses this to render a checklist
    (pre-checking the algorithm's suggestions) so the user can review and
    override which "Relevant Coursework" entries actually appear on the CV.
    """
    try:
        options = await get_course_options_for_job(job_id, session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return CourseOptionsResponse(job_id=job_id, options=options)


@router.get("/{job_id}/soft-skill-options", response_model=SoftSkillOptionsResponse)
async def get_job_soft_skill_options(job_id: int, session: AsyncSession = Depends(get_session)):
    """
    Human-in-the-loop Soft Skills picker support (mirrors /course-options):
    returns EVERY phrase in the growable soft-skill bank, each flagged with
    whether the deterministic ranker suggested it for this specific job's JD
    text, plus a match_percentage (0-100) reflecting how much of the
    phrase's own keyword set is actually mentioned in the JD. The frontend
    uses this to render a checklist (pre-checking the algorithm's
    suggestions) so the user can review and override which soft-skill
    phrases actually appear in the CV's soft_skills_line.
    """
    try:
        options = await get_soft_skill_options_for_job(job_id, session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return SoftSkillOptionsResponse(job_id=job_id, options=options)


@router.get("/{job_id}/domain-options", response_model=DomainOptionsResponse)
async def get_job_domain_options(job_id: int, session: AsyncSession = Depends(get_session)):
    """
    Human-in-the-loop "Target Role & Domains" picker support (mirrors
    /course-options and /soft-skill-options): returns EVERY entry in the
    authentic seeking-domains bank, each flagged with whether the
    deterministic ranker suggested it for this specific job's JD text, plus
    a match_percentage (0-100), AND a suggested_role string (the role phrase
    the deterministic algorithm would weave into seeking_line). The
    frontend uses this to render a checklist (pre-checking the algorithm's
    suggestions) plus an editable role-phrase text box, so the user can
    review and override the target role/domains that appear in the CV's
    seeking_line.
    """
    try:
        result = await get_domain_options_for_job(job_id, session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return DomainOptionsResponse(
        job_id=job_id,
        suggested_role=result["suggested_role"],
        options=result["options"],
    )


@router.get("/{job_id}/skill-options", response_model=SkillOptionsResponse)
async def get_job_skill_options(job_id: int, session: AsyncSession = Depends(get_session)):
    """
    Human-in-the-loop Skills picker support (mirrors /course-options,
    /soft-skill-options, /domain-options): returns EVERY skill in the
    ground-truth skills_pool.json, grouped by category, each flagged with
    whether the most recent tailoring run for this job actually included it,
    plus a match_percentage (0-100). The frontend uses this to render a
    checklist (pre-checking the previously-selected skills) so the user can
    review and override which skills from their own skill pool actually
    appear in the CV's Skills section.
    """
    try:
        options = await get_skill_options_for_job(job_id, session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return SkillOptionsResponse(job_id=job_id, options=options)


@router.post("/{job_id}/finalize-courses", response_model=FinalizeCoursesResponse)

async def finalize_job_courses(
    job_id: int,
    payload: FinalizeCoursesRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Applies the user's manually-approved course selection, soft-skill phrase
    selection, AND target role/domains selection (from the course-options /
    soft-skill-options / domain-options checklists) and re-renders the CV
    PDF, keeping the already-tailored hard skills untouched. Every selected
    course name, soft-skill phrase, and domain is re-validated against its
    respective ground-truth pool server-side -- the authenticity guardrail
    can never be bypassed by client input, even though a human explicitly
    chose these. Inserts a new CVTailored audit row (append-only history,
    same pattern as /tailor). This is the TRUE "user is done reviewing, send
    the email" checkpoint in the UI flow.
    """
    try:
        cv_tailored, email_sent = await finalize_courses_for_job(
            job_id,
            payload.course_names,
            session,
            soft_skill_phrases=payload.soft_skill_phrases,
            role_phrase=payload.role_phrase,
            selected_domains=payload.selected_domains,
            role_qualifier=payload.role_qualifier,
            semesters_remaining=payload.semesters_remaining,
            include_student_in_title=payload.include_student_in_title,
            selected_skills=payload.selected_skills,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("[jobs_router] finalize-courses failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail="Failed to finalize courses and re-render the CV.")

    tailored_fields = json.loads(cv_tailored.tailored_fields_json)

    relevant_courses = tailored_fields.get("relevant_courses", [])

    return FinalizeCoursesResponse(
        job_id=job_id,
        cv_tailored_id=cv_tailored.id,
        pdf_path=cv_tailored.pdf_path,
        relevant_courses=[
            {"name": c["name"], "grade": c["grade"], "tags": c.get("tags", []), "suggested": True}
            for c in relevant_courses
        ],
        soft_skills_line=tailored_fields.get("soft_skills_line", ""),
        seeking_line=tailored_fields.get("seeking_line", ""),
        title_line=tailored_fields.get("title_line", ""),
        email_sent=email_sent,
    )







