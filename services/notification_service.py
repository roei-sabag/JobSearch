"""
services/notification_service.py
---------------------------------
Phase 4 (scoped): Notification infrastructure only.

Sends an email (via Gmail SMTP relay, stdlib smtplib) with the tailored CV
PDF attached, immediately after a successful tailoring run. On success,
flips job.status from "tailored" -> "emailed". This module intentionally
does NOT implement any approval/token mechanism (deferred to a future phase
once the application-submission module exists) - see project_state.md for
the scoping decision.

Design guarantees:
    - This function must NEVER raise in a way that breaks an already-
      successful tailoring run. The caller (tailor_service.tailor_cv_for_job)
      wraps this call in its own isolated try/except for exactly that reason,
      but this module also fails safe internally (returns False, logs, never
      leaves job.status in an inconsistent state).
    - Blocking smtplib I/O is offloaded to a worker thread via
      asyncio.to_thread(), the same established pattern already used for
      Playwright's sync API in tailor_service.py (see project_state.md,
      Section 5, Phase 3 bugfix) - so this coroutine never blocks the
      FastAPI/uvicorn event loop.
"""

import json
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import CVTailored, Job
import asyncio


logger = logging.getLogger("notification_service")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _format_bullets(items) -> str:
    """Renders a list of strings as '  - item' lines, or a placeholder if empty."""
    if not items:
        return "  (none)"
    return "\n".join(f"  - {item}" for item in items)


def _build_email(job: Job, cv_tailored: CVTailored) -> EmailMessage:
    smtp_user = os.getenv("SMTP_USER")
    smtp_to = os.getenv("SMTP_TO_EMAIL")

    company_name = job.company.name if job.company is not None else "Unknown Company"

    msg = EmailMessage()
    msg["Subject"] = f"Tailored CV Ready: {job.title} @ {company_name}"
    msg["From"] = smtp_user
    msg["To"] = smtp_to

    # NOTE (bug fix): the job description used to be truncated to 500 chars
    # ("...Job Description Snippet..."). Per explicit instruction, the FULL
    # raw job posting text must appear in the email now -- no truncation.
    full_jd = (job.raw_description or "").strip()

    # Pull the rest of the tailoring run's stored fields (mode, dynamic CV
    # text, course selection, authenticity guardrail results, and the
    # dedicated job_analysis produced by analyze_job_posting()) out of the
    # cv_tailored audit row so the email can surface all of it, not just the
    # PDF attachment.
    try:
        fields = json.loads(cv_tailored.tailored_fields_json)
    except Exception:
        fields = {}

    mode = fields.get("mode", "unknown")
    soft_skills_line = fields.get("soft_skills_line", "")
    seeking_line = fields.get("seeking_line", "")

    relevant_courses = fields.get("relevant_courses", []) or []
    authenticity_violations = fields.get("authenticity_violations", []) or []
    course_authenticity_violations = fields.get("course_authenticity_violations", []) or []
    job_analysis = fields.get("job_analysis") or {}

    courses_block = (
        "\n".join(f"  - {c.get('name')} (Grade: {c.get('grade')})" for c in relevant_courses)
        if relevant_courses else "  (none matched this JD)"
    )

    all_violations = list(authenticity_violations) + list(course_authenticity_violations)
    violations_block = (
        "\n--- \u26a0\ufe0f Authenticity Guardrail Notes ---\n" + _format_bullets(all_violations) + "\n"
        if all_violations else ""
    )

    source_url_block = f"Source URL: {job.source_url}\n" if getattr(job, "source_url", None) else ""

    # --- Job Analysis section (from the dedicated analyze_job_posting() LLM call) ---
    if job_analysis.get("error"):
        analysis_block = f"(Job analysis unavailable: {job_analysis['error']})\n"
    elif job_analysis:
        analysis_block = (
            f"Role Summary:\n  {job_analysis.get('role_summary', '(n/a)')}\n\n"
            f"Seniority Assessment:\n  {job_analysis.get('seniority_assessment', '(n/a)')}\n\n"
            f"Must-Have Requirements:\n{_format_bullets(job_analysis.get('must_have_requirements', []))}\n\n"
            f"Nice-to-Have Requirements:\n{_format_bullets(job_analysis.get('nice_to_have_requirements', []))}\n\n"
            f"Key Technologies Emphasized:\n{_format_bullets(job_analysis.get('key_technologies', []))}\n\n"
            f"Fit Assessment (vs. your real background):\n  {job_analysis.get('fit_assessment', '(n/a)')}\n\n"
            f"Red Flags / Things to Watch For:\n{_format_bullets(job_analysis.get('red_flags', []))}\n\n"
            f"Suggested Interview Questions to Ask:\n{_format_bullets(job_analysis.get('suggested_interview_questions', []))}\n"
        )
    else:
        analysis_block = "(No job analysis was recorded for this tailoring run.)\n"

    body = (
        f"A new tailored CV has been generated for the following job posting:\n\n"
        f"Job ID:  {job.id}\n"
        f"Title:   {job.title}\n"
        f"Company: {company_name}\n"
        f"Status:  {job.status}\n"
        f"{source_url_block}"
        f"Tailoring mode: {mode}\n"
        f"Generated: {cv_tailored.created_at}\n\n"
        f"=== Final CV Summary Fields ===\n"
        f"Soft skills line: {soft_skills_line}\n"
        f"Seeking line:     {seeking_line}\n\n"

        f"Relevant Coursework included on CV:\n{courses_block}\n"
        f"{violations_block}\n"
        f"=== \U0001f50d Job Analysis ===\n\n"
        f"{analysis_block}\n"
        f"=== Full Job Description (as submitted) ===\n"
        f"{full_jd}\n\n"
        f"The full tailored CV PDF is attached to this email.\n\n"
        f"(This is an automated notification. No application has been submitted -- "
        f"this system never auto-applies on your behalf.)\n"
    )
    msg.set_content(body)

    pdf_path = Path(cv_tailored.pdf_path)
    if pdf_path.exists():
        pdf_bytes = pdf_path.read_bytes()
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=pdf_path.name,
        )
    else:
        logger.warning(
            "[notification_service] PDF path %s does not exist; sending email without attachment.",
            pdf_path,
        )

    return msg


def _send_email_sync(msg: EmailMessage) -> None:
    """Blocking SMTP send - must only ever be called via asyncio.to_thread()."""
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_APP_PASSWORD")

    if not smtp_user or not smtp_password:
        raise RuntimeError(
            "SMTP_USER / SMTP_APP_PASSWORD not set in environment; cannot send notification email."
        )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


async def send_tailoring_notification(
    job_id: int, cv_tailored_id: int, session: AsyncSession
) -> bool:
    """
    Loads the Job + CVTailored records, composes and sends the notification
    email with the tailored PDF attached, and on success flips
    job.status = "emailed" (committed).

    Returns True on successful send + status update, False on any failure
    (fully logged, never raised past this function under normal operation --
    the one exception is if job_id/cv_tailored_id themselves don't resolve,
    which indicates a genuine programming error upstream and is re-raised
    after logging so it's not silently swallowed).
    """
    # NOTE: eager-load job.company via selectinload() rather than
    # session.get(Job, job_id) - accessing a lazy relationship attribute
    # (job.company) later inside the sync _build_email() call would trigger
    # an implicit lazy-load, which raises MissingGreenlet under SQLAlchemy's
    # async engine (lazy-loading is only safe inside an active async
    # context/awaitable, never from plain synchronous attribute access).
    result = await session.execute(
        select(Job).options(selectinload(Job.company)).where(Job.id == job_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job with id={job_id} not found")

    cv_tailored = await session.get(CVTailored, cv_tailored_id)
    if cv_tailored is None:
        raise ValueError(f"CVTailored with id={cv_tailored_id} not found")


    try:
        msg = _build_email(job, cv_tailored)
        await asyncio.to_thread(_send_email_sync, msg)
    except Exception:
        logger.exception(
            "[notification_service] Failed to send tailoring notification for job_id=%s "
            "(cv_tailored_id=%s). job.status remains unchanged ('%s').",
            job_id, cv_tailored_id, job.status,
        )
        return False

    job.status = "emailed"
    await session.commit()
    logger.info(
        "[notification_service] Notification email sent successfully for job_id=%s; "
        "status updated to 'emailed'.",
        job_id,
    )
    return True
