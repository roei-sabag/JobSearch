# Project State & Handover Documentation

**Last updated:** End of Phase 3.5 (intelligent raw-text ingestion + one-click minimalist Web UI, verified end-to-end) / eve of Phase 4 (HITL notification loop).


**Purpose of this file:** Give a fresh chat session 100% architectural alignment to resume work immediately, with zero re-discovery cost.

---

## 1. Project Core Objective

**Autonomous AI-Driven Job Search & Resume Tailoring System** for Roei Sabag, a 4th-year
B.Sc. Electrical & Computer Engineering student at Ben-Gurion University of the Negev
(Communications & Computer Systems track), targeting **student/internship positions** in:
- Hardware Verification
- DFT (Design for Test)
- RTL Design / FPGA Development / Digital Design
- Communication Systems / Signal Processing / Computer Architecture
- General "communication-related hardware roles"

The system's end goal (full vision, phases beyond what's built today):
1. **Module A (future):** Autonomous scraping of LinkedIn, job boards, and ATS career
   pages (Greenhouse/Lever/Comeet), with dynamic discovery of new career sites, filtered
   strictly for student/intern/junior roles.
2. **Module B (built - Phase 2):** Manual ingestion port for jobs sourced from
   WhatsApp/Discord groups (bypasses the scraper).
3. **Module C (built - Phase 1/1.5, being wired into backend in Phase 3):** Generative
   AI resume tailoring with **strict layout preservation** — the LLM only swaps dynamic
   text (skills selection, summary phrasing, relevant coursework), never the HTML/CSS
   structure — rendered to a pixel-accurate PDF.
4. **Module D (future - Phase 4):** Human-in-the-loop notification loop. The system must
   **never auto-apply**. It emails the user the job + tailored CV PDF, and waits for
   explicit approval before marking a job "Ready to Apply".

**Non-negotiable content guardrail (applies to all phases):** The system must NEVER
fabricate skills, courses, grades, employers, dates, or credentials beyond what exists in
the ground-truth source files (`all_my_skills.txt`, `Roei_Sabag_CV.pdf`, `skills_pool.json`,
`courses_pool.json`). This is enforced via explicit `validate_authenticity()` /
`validate_courses_authenticity()` guardrail functions that strip any fabricated content
before rendering, and is documented as binding law in `cv_rendering_rules.md`.

---

## 2. Current Tech Stack & Infrastructure

| Concern | Choice | Why |
|---|---|---|
| Backend API | **FastAPI** | Async-native, auto OpenAPI docs, clean DI via `Depends`. |
| Server | **uvicorn** (`uvicorn api.main:app --reload`) | Standard ASGI server for FastAPI. |
| Database | **SQLite** (`jobs.db`) via **SQLAlchemy 2.0 Async ORM** + **aiosqlite** driver | Zero-setup local dev; because *all* access goes through the async ORM API, swapping to PostgreSQL later requires only changing `DATABASE_URL` (e.g. to `postgresql+asyncpg://...`) — no model/query code changes. |
| LLM | **Anthropic Claude** (env var `ANTHROPIC_API_KEY`) | Used to select/prioritize skills and phrase summary fields, constrained to a ground-truth pool. |
| LLM fallback | **Deterministic local keyword-overlap ranker** (`tailor_with_fallback` in `tailor_skills.py`) | Used automatically if no API key is set OR if the Anthropic call fails (e.g. current known issue: `claude-3-5-sonnet-20241022` model string returns 404 in this environment — needs a valid model name or API access fix in a future session). System never crashes; it always degrades gracefully to the deterministic ranker. |
| PDF rendering | **Playwright (headless Chromium) print-to-PDF** — **NOT WeasyPrint** | **Explicit architecture decision (confirmed with user):** WeasyPrint requires an external GTK3 runtime on Windows (not a clean `pip install`), and its CSS engine differs from Chromium's, risking silently breaking the already-QA-validated flex/gap layout. Playwright/Chromium is the proven, validated engine — stick with it for all future PDF generation (including the Phase 3 service layer). |
| Templating | **Jinja2** over a frozen HTML/CSS template (`cv_template.html` + `cv_style.css`) | Only placeholders (`{{ }}` / `{% %}`) are dynamic; document structure, section order, and typography are frozen per `cv_rendering_rules.md`. |
| PDF text extraction / verification | **PyMuPDF (`fitz`)** for reading back rendered PDF text; **pdfplumber** (word-position based extraction) for precisely parsing the academic transcript PDF (`rwservlet.pdf`) into `courses_pool.json`. | pdfplumber's word-position grouping was necessary because naive text extraction destroyed the course↔grade column alignment. |
| Env/config | **python-dotenv** (`.env` file, currently holds `ANTHROPIC_API_KEY`) | |
| Validation | **Pydantic** (v2 style, `field_validator`) | Both for FastAPI request/response schemas (`api/schemas.py`) and for the LLM's structured output contract (`TailoredSkillsResponse` in `tailor_skills.py`). |

---

## 3. Database Schema Blueprint (current + Phase 3 additions)

### Currently implemented (`db/models.py`, Phase 2 — DONE):

```python
class Company(Base):
    __tablename__ = "companies"
    id: int (PK, autoincrement)
    name: str (unique=True, index=True, nullable=False)
    career_url: str | None
    created_at: datetime (server_default=now)

    jobs: relationship -> list[Job]


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_jobs_content_hash"),)

    id: int (PK, autoincrement)
    title: str (512)
    company_id: int | None (FK -> companies.id, index=True, nullable=True)
    raw_description: Text (nullable=False)
    source: str (64, index=True, nullable=False)       # e.g. 'manual_whatsapp', 'manual_discord', 'linkedin'
    source_url: str | None (1024)
    content_hash: str (64, unique=True, index=True, nullable=False)  # SHA-256(title+company+description), normalized
    status: str (32, index=True, default="scraped")    # 'scraped' -> 'tailored' -> 'emailed' -> 'approved' -> 'applied'
    created_at: datetime (server_default=now)
    updated_at: datetime (onupdate=utcnow, server_default=now)

    company: relationship -> Company | None
```

**Deduplication logic** (`compute_content_hash()` in `db/models.py`): normalizes
(lowercase, strip, collapse whitespace) title + company_name + raw_description, then
SHA-256 hashes the joined string. The `/api/jobs/ingest` endpoint checks this hash before
insert and returns the existing record with `is_duplicate: true` instead of creating a
duplicate row or raising an error (duplicate WhatsApp re-shares are an expected, harmless
occurrence).

### Implemented in Phase 3 (`db/models.py`, DONE):

```python
class CVMaster(Base):
    __tablename__ = "cv_master"
    id: int (PK)
    version_label: str            # e.g. "v1-frozen-layout" (get-or-create key)
    html_template_path: str       # points at "cv_template.html" (absolute path)
    raw_text: str                 # ground-truth extracted facts (from all_my_skills.txt) for LLM context/audit
    created_at: datetime

    tailored_versions: relationship -> list[CVTailored]


class CVTailored(Base):
    __tablename__ = "cv_tailored"
    id: int (PK)
    job_id: int (FK -> jobs.id, index=True)
    cv_master_id: int (FK -> cv_master.id)
    tailored_fields_json: Text    # full audit JSON: mode, categories, opening_phrase, seeking_line,
                                   #   rationale, omitted, relevant_courses, authenticity_violations,
                                   #   course_authenticity_violations
    pdf_path: str                 # e.g. "output/cvs/tailored_job_{job_id}.pdf" (absolute path)
    created_at: datetime

    job: relationship -> Job
    cv_master: relationship -> CVMaster
```

Index confirmed: `CVTailored.job_id` (frequent query: "give me all tailored versions for job X").
`Job.tailored_versions` relationship added on the `Job` side for the reverse lookup used by
`GET /api/jobs/{id}` and `GET /api/jobs`.


---

## 4. Directory Structure & File Map

```
JobSearch/
├── .env                        # ANTHROPIC_API_KEY (and future secrets, e.g. SMTP creds for Phase 4)
├── requirements.txt            # fastapi, uvicorn[standard], sqlalchemy>=2.0, aiosqlite, greenlet,
│                                #   pydantic, python-dotenv, jinja2, playwright, anthropic, pdfplumber, PyMuPDF
├── jobs.db                     # SQLite database file (auto-created by db/init_db.py on startup)
│
├── db/                         # Database layer (Phase 2 + Phase 3, DONE)
│   ├── __init__.py
│   ├── base.py                 # Async SQLAlchemy engine + AsyncSessionLocal + get_session() FastAPI dependency
│   ├── models.py                # Company, Job, CVMaster, CVTailored ORM models + compute_content_hash()
│   └── init_db.py              # init_models(): idempotent create_all(), called on FastAPI startup via lifespan
│
├── api/                        # FastAPI application (Phase 2 + Phase 3 + Phase 3.5, DONE)
│   ├── __init__.py
│   ├── main.py                  # FastAPI app instance, lifespan startup (calls init_models + mkdir output/cvs), /health,
│                                  #   includes jobs router, mounts /files/cvs (static PDF serving) and / (static Web UI, last)
│   ├── schemas.py               # Pydantic: JobIngestRequest/Response, JobOut, CVTailoredOut, TailorJobResponse,
│                                  #   JobIngestRawRequest/Response (Phase 3.5)
│   ├── deps.py                   # Re-exports get_session for router use
│   └── routers/
│       ├── __init__.py
│       └── jobs.py               # POST /api/jobs/ingest, POST /api/jobs/ingest-raw (Phase 3.5, LLM extraction + auto-tailor),
│                                  #   GET /api/jobs/{id} (incl. tailored_versions), GET /api/jobs,
│                                  #   POST /api/jobs/{job_id}/tailor (BackgroundTasks trigger, DONE Phase 3)
│
├── services/                   # Phase 3 + Phase 3.5, DONE
│   ├── tailor_service.py        # ensure_cv_master_seeded(session); tailor_cv_for_job(job_id, session) --
│   │                             # reuses tailor_skills.py's LLM/fallback/guardrail/render functions rather than duplicating them;
│   │                             # wraps the sync-Playwright render_pdf() call in asyncio.to_thread() (required fix, see Section 5)
│   └── ingestion_service.py     # (Phase 3.5) extract_job_fields_from_raw_text(raw_text) -> (ExtractedJobFields, mode) --
│                                 # Anthropic (claude-3-5-haiku) extraction with strict extract-never-fabricate prompt,
│                                 # deterministic dependency-free fallback if no API key / LLM call fails or malforms JSON
│
├── output/                      # Created dynamically on FastAPI startup AND by tailor_service.py (mkdir idempotent)
│   └── cvs/                      # tailored_job_{job_id}.pdf + _rendered_job_{job_id}.html land here; served at /files/cvs/*
│
├── static/                      # (Phase 3.5) One-click minimalist Web UI, served by FastAPI's StaticFiles
│   └── index.html                # Single-file vanilla JS/CSS UI: textarea + submit button + live polling status log
│

├── tailor_skills.py             # Phase 1/1.5 standalone CV-tailoring engine/script (STILL the source of truth for tailoring logic,
│                                 #   now also imported/reused by services/tailor_service.py -- not duplicated):
│                                 #   - Loads sample_jd.txt + skills_pool.json + courses_pool.json
│                                 #   - tailor_with_anthropic() / tailor_with_fallback(): pick+prioritize skills, compose
│                                 #     opening_phrase + seeking_line from strictly authentic trait/domain banks
│                                 #   - validate_authenticity(): strips any fabricated skill/trait/domain
│                                 #   - select_relevant_courses() + validate_courses_authenticity(): deterministic (never LLM),
│                                 #     picks courses graded 80-100 that tag-match the JD; empty list => no Coursework block rendered
│                                 #   - render_pdf(output_pdf_path=None, rendered_html_path=None, template_path=None): Jinja2 render +
│                                 #     Playwright (SYNC API) print-to-PDF -> tailored_cv.pdf by default, or job-specific paths when
│                                 #     called with overrides from tailor_service.py. NOTE: sync Playwright must be called via
│                                 #     asyncio.to_thread() when invoked from async FastAPI code (see Section 5, Phase 3 bugfix).
│                                 #   - diff_against_previous() / save_current_state(): tracks changes run-to-run (cv_dynamic_state.json)
│                                 #   - write_report(): writes tailoring_report.md (rationale, violations, coursework, skills)
│

├── cv_template.html              # FROZEN Jinja2 HTML template — the exact visual layout of Roei's CV.
│                                  #   Only placeholders change: {{ opening_phrase }}, {{ seeking_line }}, tailored_skills loop,
│                                  #   {% if relevant_courses %} coursework block. Structure/section order is frozen (see rules doc).
├── cv_style.css                   # FROZEN CSS for the template (absolute pt/cm units only, no justify, see rules doc).
├── cv_rendering_rules.md          # "Engineering Constitution" — binding rules for ALL future rendering/tailoring/QA code:
│                                  #   typography constraints, content/authenticity guardrails, overflow protections,
│                                  #   the Relevant Coursework conditional-rendering rule (section 3a), and Visual-QA-loop governance.
├── visual_qa_loop.py               # Phase 1.5 vision-LLM-driven auto-correction loop: renders PDF -> screenshot -> compares
│                                   #   to original CV visually -> proposes targeted CSS patches (selector/property/value) only;
│                                   #   never touches HTML structure or textual content. Logs each patch to _css_history/.
├── qa_report.md                    # Output of the last visual_qa_loop.py run (similarity scores, patches applied).
├── _css_history/                    # Snapshots of cv_style.css at each auto-correction iteration (audit trail).
│
├── all_my_skills.txt               # Ground-truth full skills inventory (superset of skills_pool.json).
├── skills_pool.json                # Curated/categorized ground-truth skills pool actually used for CV tailoring
│                                    #   (categories: "Hardware & Verification", "Programming & Tools" — Engineering Fields
│                                    #   category and 4 specific skills were deliberately removed per user request).
├── courses_pool.json                # Ground-truth academic transcript data (course name + exact grade + relevance tags),
│                                    #   extracted via word-position-accurate pdfplumber parsing of rwservlet.pdf.
│                                    #   ONLY courses graded 80-100 are included (Pass/Exempt/400-code/F grades excluded).
│                                    #   'tags' are curated for JD-matching only (never raw course-name word matching,
│                                    #   to avoid false positives like "Physics 1A" matching "Electrical Engineering" JDs).
├── Roei_Sabag_CV.pdf                 # Original baseline Master CV (source of ground truth for experience/projects/education).
├── rwservlet.pdf                     # Official BGU academic transcript PDF (source for courses_pool.json).
├── sample_jd.txt                     # Sample job description used for local testing of tailor_skills.py.
│
├── cv_dynamic_state.json             # State snapshot of the last tailor_skills.py run (for diffing changes across runs).
├── tailoring_report.md               # Human-readable rationale report from the last tailor_skills.py run.
├── tailoring_report_full.md          # (Earlier/reference version of the report.)
├── tailored_cv.pdf                    # The actual rendered output PDF from the last tailor_skills.py run.
└── _rendered_cv.html                  # Intermediate HTML file written by render_pdf() before Playwright converts it to PDF.
```

---

## 5. Current Milestone Status

- ✅ **Phase 1 & 1.5 (Visual QA & Tailoring Engine): COMPLETED and verified.**
  - `tailor_skills.py` fully implements LLM-driven (with deterministic fallback) skill
    selection, authentic summary phrasing, and — added in a later iteration — deterministic
    Relevant Coursework selection from the real academic transcript (grades 80-100 only,
    tag-based JD relevance matching, guardrails against fabrication/misattribution).
  - `visual_qa_loop.py` implements the vision-LLM auto-correction loop with strict
    governance (only targeted CSS patches, logged history, stop conditions).
  - `cv_rendering_rules.md` codifies all layout/content/overflow/QA-governance rules as
    binding law for all future automation.
  - Verified: rendered PDF is exactly 1 page, layout preserved, skills/coursework
    correctly tailored to `sample_jd.txt` (Hardware Verification JD) after fixing a real
    tokenization bug (regex `[a-zA-Z+/#]+` was treating "Python/C/C++" as one glob token,
    causing course-tag matches like "python"/"c" to silently fail — fixed to `[a-zA-Z]+`
    tokenization on both sides of the JD/tag comparison).

- ✅ **Phase 2 (Async SQLite & Ingestion API): COMPLETED and verified.**
  - `db/base.py`, `db/models.py` (Company, Job), `db/init_db.py` built with async
    SQLAlchemy 2.0 + aiosqlite.
  - `api/main.py`, `api/schemas.py`, `api/deps.py`, `api/routers/jobs.py` built with
    FastAPI: `POST /api/jobs/ingest` (dedup via SHA-256 `content_hash`, case-insensitive
    get-or-create Company), `GET /api/jobs/{id}`, `GET /api/jobs` (status filter).
  - End-to-end tested live against a running uvicorn server: job creation (job_id=1),
    duplicate detection (`is_duplicate: true`, no duplicate row created), GET by id, GET
    list — all confirmed working correctly.
  - `requirements.txt` created with the full dependency list.

- ✅ **Phase 3 (Backend Integration & BackgroundTasks): COMPLETED and verified.**
  - `db/models.py` extended with `CVMaster` (id, version_label, html_template_path,
    raw_text, created_at) and `CVTailored` (id, job_id FK indexed, cv_master_id FK,
    tailored_fields_json, pdf_path, created_at); `Job.tailored_versions` relationship added.
  - `services/tailor_service.py` created: `ensure_cv_master_seeded()` get-or-creates the
    single `CVMaster` row (raw_text seeded from `all_my_skills.txt`); `tailor_cv_for_job()`
    reuses (does not duplicate) `tailor_skills.py`'s `tailor_with_anthropic`/
    `tailor_with_fallback`/`validate_authenticity`/`select_relevant_courses`/
    `validate_courses_authenticity`/`render_pdf` functions, rendering to
    `output/cvs/tailored_job_{job_id}.pdf`, inserting a `CVTailored` audit row, and setting
    `job.status = "tailored"` (or `"tailor_failed"` on any exception, always logged, never
    silent).
  - `tailor_skills.py`'s `render_pdf()` was refactored to accept optional
    `output_pdf_path`/`rendered_html_path`/`template_path` overrides (defaults preserved for
    the standalone script) so it can be safely reused for job-specific output paths.
  - **Critical bug found & fixed during verification:** Playwright's **sync API** (used by
    `render_pdf()`) cannot run inside FastAPI/uvicorn's already-active asyncio event loop
    (raises `playwright._impl._errors.Error: ... using Playwright Sync API inside the
    asyncio loop`). Fixed by wrapping the `render_pdf()` call in
    `await asyncio.to_thread(...)` inside `tailor_cv_for_job()`, so it runs in a worker
    thread with no running loop. **This is now a settled, binding pattern for any future
    code that calls Playwright's sync API from within an async FastAPI code path.**
  - `api/routers/jobs.py`: added `POST /api/jobs/{job_id}/tailor` — 404s if job missing,
    otherwise `background_tasks.add_task(...)` a wrapper (`_run_tailoring_background`) that
    opens its own fresh `AsyncSessionLocal()` (never reuses the request-scoped `Depends`
    session, per the settled design decision), returns `202` immediately with
    `{"status": "processing", "message": "...", "job_id": ...}`.
  - `api/schemas.py`: added `CVTailoredOut` (id, pdf_path, created_at) and
    `TailorJobResponse`; `JobOut` extended with `tailored_versions: List[CVTailoredOut]`,
    populated in both `GET /api/jobs/{id}` and `GET /api/jobs` via a shared
    `_get_tailored_versions()` helper.
  - `output/cvs/` directory created dynamically/idempotently via `Path.mkdir(parents=True,
    exist_ok=True)` inside `tailor_cv_for_job()` — no manual setup needed.
  - **End-to-end verification performed live against a running uvicorn server:**
    ingested a fresh job (job_id=2, "FPGA Verification Intern" JD mentioning SystemVerilog/
    testbench/Vivado/Python), triggered `POST /api/jobs/2/tailor` (got immediate `202`),
    polled `GET /api/jobs/2` until `status == "tailored"` with `tailored_versions`
    populated pointing at `output/cvs/tailored_job_2.pdf`. Confirmed via PyMuPDF (`fitz`)
    that the PDF is exactly 1 page and its content is genuinely tailored to that JD (e.g.
    seeking_line correctly reads "Seeking a student position in FPGA Development, Hardware
    Verification, or communication-related hardware roles."). Also re-confirmed
    ingest-dedup still works correctly post-change (re-posting the same job returned
    `is_duplicate: true` with the existing, now-`"tailored"`, job record, no duplicate row).
  - Known pre-existing issue confirmed still present (not a regression): the Anthropic call
    still 404s on `claude-3-5-sonnet-20241022` in this environment; the pipeline gracefully
    falls back to the deterministic local ranker exactly as designed, and this was the mode
    exercised during the Phase 3 verification run above.

- ✅ **Phase 3.5 (Intelligent Raw-Text Ingestion + One-Click Minimalist Web UI): COMPLETED
  and verified.**
  - `services/ingestion_service.py` created: `ExtractedJobFields` Pydantic schema (title,
    company_name, cleaned_description) + `extract_job_fields_from_raw_text(raw_text)` —
    calls Anthropic (small/fast model `claude-3-5-haiku-20241022`, deliberately separate
    from whatever model tailoring uses) with a strict "extract-never-fabricate" system
    prompt, with a deterministic dependency-free fallback (`_extract_with_fallback`, first
    plausible-length line -> title guess, rest -> description, light noise-stripping)
    used automatically if no `ANTHROPIC_API_KEY` is set or the LLM call fails/malforms its
    JSON — same graceful-degradation guarantee already established in `tailor_skills.py`,
    this endpoint must NEVER 500 due to LLM unavailability.
  - `api/routers/jobs.py` refactored: extracted the shared get-or-create-company +
    dedup-check + insert logic out of `ingest_job()` into a private `_create_job_record(...)`
    helper (returns a small `_JobRecordResult` carrier), so `/ingest` and the new
    `/ingest-raw` never drift apart. Confirmed via regression test that `/ingest` behaves
    identically post-refactor (dedup + fresh insert both still correct).
  - New endpoint `POST /api/jobs/ingest-raw` added: accepts `{"raw_text": "..."}` only,
    calls `extract_job_fields_from_raw_text()`, feeds the result into
    `_create_job_record(...)` with `source="manual_raw_text"`, and — if not a duplicate —
    immediately `background_tasks.add_task(_run_tailoring_background, job.id)` (reusing the
    exact same Phase 3 background-tailoring wrapper, unchanged). Returns `202` with
    `JobIngestRawResponse` (job_id, title, company_name, status, content_hash, is_duplicate,
    extraction_mode, created_at) — `extraction_mode` is one of `"llm"`, `"fallback"`, or
    `"fallback (after LLM error)"` for full transparency into which path was used.
  - `api/schemas.py` extended with `JobIngestRawRequest`/`JobIngestRawResponse`.
  - `api/main.py`: added `app.mount("/files/cvs", StaticFiles(directory=output/cvs))` so
    generated tailored-CV PDFs are directly browser-linkable (e.g.
    `GET /files/cvs/tailored_job_3.pdf`), and `app.mount("/", StaticFiles(directory=static,
    html=True))` **mounted last** (so it never shadows the `/api/...` routes) to serve the
    new one-click Web UI. `output/cvs/` is now also `mkdir(parents=True, exist_ok=True)`'d
    on FastAPI startup (in `lifespan`), not just lazily inside the tailoring service, so the
    static mount never 404s on a missing directory before the first tailoring run.
  - **UI tech stack decision (settled):** plain static `static/index.html` (vanilla JS,
    inline CSS, zero build step, zero new dependency) served via FastAPI's `StaticFiles`,
    chosen over Streamlit specifically to keep this a **single-process, single-port**
    local app (`http://127.0.0.1:8000/`) — matching this project's consistent
    minimal-dependency philosophy. `static/index.html` provides exactly the requested UI:
    one large textarea ("Paste Raw Job Description Here..."), one button ("Submit &
    Generate Tailored CV"), and a live status log (polls `GET /api/jobs/{id}` every ~2s)
    that shows "Job Ingested (ID: N) -> Extracted Intel -> Tailoring CV in Background ->
    Complete: View PDF" with a working link to the generated PDF via the `/files/cvs/`
    mount.
  - **End-to-end verification performed live against a running uvicorn server:** confirmed
    `GET /` and `GET /index.html` both serve the UI (`200`); posted a deliberately messy,
    WhatsApp-style raw text blob (forwarding banner, emoji, hashtags, an "apply here" link,
    embedded "Company: ChipWorks Ltd" line) to `POST /api/jobs/ingest-raw` — the Anthropic
    extraction call 404'd in this environment (same pre-existing model-name issue as
    Phase 3) and the pipeline correctly degraded to `extraction_mode: "fallback (after LLM
    error)"` without erroring; the job was still created, the background tailoring task
    still fired automatically, and polling `GET /api/jobs/{id}` showed `status ==
    "tailored"` with a `tailored_versions` entry. Confirmed via PyMuPDF that the resulting
    1-page PDF's `seeking_line` correctly reflects "RTL Design" pulled from the raw text
    (proving the extracted `cleaned_description` genuinely flowed through to the existing
    Phase 3 tailoring pipeline unmodified). Confirmed `GET /files/cvs/tailored_job_3.pdf`
    returns `200` (servable/linkable). Confirmed re-posting the identical raw text a second
    time correctly returned `is_duplicate: true` (no duplicate row, no duplicate background
    tailoring task fired) — same dedup guarantee as `/ingest`, now proven to also hold for
    `/ingest-raw`. Also re-confirmed `POST /api/jobs/ingest` (the original endpoint) still
    works identically post-refactor — zero regression on existing endpoints.
  - No new dependencies were required (`requirements.txt` unchanged — reuses the
    already-installed `anthropic`, `fastapi`, `pydantic`).


---

## 6. Current Milestone Status (continued) — Phase 4 (scoped: Notification Infrastructure)

- ✅ **Phase 4 (SCOPED — Notification Infrastructure only): COMPLETED and verified.**
  - **Scope decision (explicit, made with user):** the full HITL approval mechanism
    (signed tokens, `/approve` endpoint, `"approved"` status, confirmation page) was
    **deliberately deferred**, since the system doesn't yet have an application-submission
    module for an approval to meaningfully gate. Building the approval gate before there's
    anything to approve *into* would have been premature/speculative scope. This iteration
    covers ONLY: SMTP config, sending the notification email with the tailored PDF
    attached, and flipping `job.status` to `"emailed"` on success.
  - `.env` extended with `SMTP_USER`, `SMTP_APP_PASSWORD` (Gmail App Password, NOT the
    regular account password), `SMTP_TO_EMAIL` — same pattern as existing `ANTHROPIC_API_KEY`
    handling. A `.gitignore` was also created (didn't exist before) with `.env` as its first
    entry, since this was the first phase adding a new sensitive credential.
  - `services/notification_service.py` created (new module):
    - `send_tailoring_notification(job_id, cv_tailored_id, session) -> bool` — eager-loads
      `Job` with `selectinload(Job.company)` (NOT `session.get(Job, job_id)` — a lazy
      relationship access like `job.company` later inside the sync email-building code
      would raise `MissingGreenlet` under the async SQLAlchemy engine; this was caught and
      fixed during implementation, not left as a latent bug).
    - Builds a plain-text `EmailMessage` (subject, job title/company/status, a JD snippet,
      and an explicit reminder line: "No application has been submitted -- this system
      never auto-applies on your behalf"), attaches the PDF bytes read directly from
      `CVTailored.pdf_path`.
    - Blocking `smtplib.SMTP(...).starttls().login().send_message()` calls wrapped in
      `asyncio.to_thread(...)` — same established pattern as the Playwright-sync-in-async-
      loop fix from Phase 3.
    - **On success:** sets `job.status = "emailed"`, commits, returns `True`.
    - **On ANY exception** (bad credentials, network failure, etc.): logs the full
      traceback via `logger.exception(...)` (never silent), leaves `job.status` untouched
      (still `"tailored"` — NOT incorrectly downgraded to `"tailor_failed"`, since tailoring
      itself already succeeded), returns `False`.
  - `services/tailor_service.py::tailor_cv_for_job()` wired to call
    `send_tailoring_notification(...)` automatically, immediately after the existing
    `job.status = "tailored"` commit + refresh — wrapped in its OWN isolated `try/except`
    so that any notification failure can never break, roll back, or mask the
    already-successful tailoring result. This isolation is the single most important
    safety property of this phase and was explicitly verified (see below).
  - No new pip dependency was required (`smtplib`/`email.message` are Python stdlib);
    `requirements.txt` is unchanged.
  - **End-to-end verification performed live (real SMTP send, not mocked):**
    1. Ingested a fresh test job via the real `/ingest-raw` pipeline path
       (`extract_job_fields_from_raw_text` → `_create_job_record` → `tailor_cv_for_job`).
       Confirmed tailoring completed successfully and `job.status` ended at `"emailed"`.
    2. **User confirmed via direct inbox check** that a real email arrived at
       `roeisabag4475@gmail.com` with the correct subject
       (`"Tailored CV Ready: {title} @ {company}"`) and the tailored CV PDF attached
       correctly.
    3. **Failure-path test:** deliberately set `SMTP_APP_PASSWORD` to an invalid value
       before running the pipeline again. Confirmed: (a) `smtplib.SMTPAuthenticationError`
       was raised and fully logged (not swallowed silently), (b) tailoring still completed
       successfully end-to-end (new `CVTailored` row created, PDF rendered), (c)
       `job.status` correctly remained `"tailored"` — proving the isolation guarantee holds
       under a real failure, not just in theory.
    4. **Regression suite re-run:** re-posted an existing job's exact content via
       `_create_job_record()` twice in a row — confirmed `is_duplicate: True` on the second
       call with no duplicate row created (dedup logic unaffected by Phase 4 changes).
       Re-called `get_course_options_for_job()` for an existing job — confirmed it still
       returns the full course list with correct `suggested` flags, unaffected by the new
       notification step. Zero regressions found on any Phase 1–3.5 functionality.

---

## 7. Next Immediate Action Items (resume here in the next session, in Act Mode)

Phase 4 (scoped) is complete and verified. The deferred, NOT-yet-built scope from the
original Phase 4 plan remains the next logical increment, but should only be picked up
**once there is an actual application-submission module for it to gate**:

1. **Approval mechanism** (deferred): `job.status` lifecycle extension to
   `... -> "emailed" -> "approved"`; a signed-token approval link (e.g. via `itsdangerous`,
   `URLSafeTimedSerializer`) embedded in the notification email; a new
   `GET /api/jobs/{job_id}/approve?token=...` endpoint that verifies the signature +
   job_id binding, flips `status -> "approved"`, and returns a minimal inline-HTML
   confirmation page reiterating that the system still does not auto-apply. This was
   scoped OUT of the current iteration on purpose — see Section 6 above for the reasoning
   (no approval target yet).
2. Whatever comes after Module D per Section 1's original vision (Module A: autonomous
   scraping) remains fully unscoped and undiscussed as of this session.

**Important reminder for the next session:** before building the approval/token mechanism,
confirm with the user that an application-submission (or at least application-tracking)
module is actually being planned next — otherwise the approval gate has nothing meaningful
to gate into yet, per the explicit scoping decision made in this phase.



  
## Git Sync Test (Windows to Pi) - 14/07/2026 14:25:56.10  
