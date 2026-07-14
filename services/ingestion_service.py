"""
services/ingestion_service.py
-------------------------------
Intelligent raw-text job ingestion: takes a single messy blob of text (e.g.
copy-pasted from WhatsApp or LinkedIn) and extracts structured job fields
(title, company_name, cleaned_description) using an LLM, with a deterministic
fallback so this endpoint NEVER 500s just because the LLM/API is unavailable
-- mirroring the exact graceful-degradation pattern already proven in
tailor_skills.py (tailor_with_anthropic / tailor_with_fallback).

This module has ONE job: raw text -> ExtractedJobFields. It knows nothing
about the DB or the tailoring pipeline; api/routers/jobs.py wires the output
of this module into the existing job-creation + background-tailoring flow.
"""

import json
import logging
import os
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("ingestion_service")

# Small/fast/cheap model for extraction -- deliberately different from
# whatever model constant tailor_skills.py uses for full tailoring, since
# this is a lightweight classification/extraction task, not creative writing.
# NOTE (bug fix): "claude-3-5-haiku-20241022" has been retired by Anthropic
# (confirmed via the same live 404 not_found_error class of failure seen with
# tailor_skills.py's ANTHROPIC_MODEL). This silently degraded every /ingest-raw
# call to the deterministic fallback extractor. Updated to a current, valid
# Haiku model ID.
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"



UNKNOWN_COMPANY = "Unknown/Inferred"
UNCLEAR_TITLE = "Unclear/Not a job posting"


class ExtractedJobFields(BaseModel):
    title: str = Field(..., description="Inferred professional job title")
    company_name: str = Field(default=UNKNOWN_COMPANY)
    cleaned_description: str = Field(
        ..., min_length=1,
        description="The actual job duties/requirements text, with noise "
                     "(headers, emojis, 'apply here' links, forwarded-message "
                     "banners) stripped out.",
    )

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        return v if v else "Untitled Position"

    @field_validator("company_name")
    @classmethod
    def company_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        return v if v else UNKNOWN_COMPANY

    @field_validator("cleaned_description")
    @classmethod
    def description_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("cleaned_description must not be blank")
        return v


def _extract_with_anthropic(raw_text: str) -> ExtractedJobFields:
    import anthropic

    client = anthropic.Anthropic()

    system_prompt = """You are a precise information-extraction assistant. You will be given a raw,
possibly messy block of text (e.g. copy-pasted from a WhatsApp group, LinkedIn post, or
forwarded email) that is SUPPOSED to describe a job opening. Your job is to extract exactly
three fields and return them as a single valid JSON object with this schema:

{
  "title": "<a concise, professional job title, inferred from context if not explicit>",
  "company_name": "<the hiring company's name, or exactly the string \"Unknown/Inferred\" if it cannot be determined>",
  "cleaned_description": "<the actual job responsibilities/requirements text, with clutter removed>"
}

CRITICAL RULES:
- You may INFER a professional title/label from context (this is a categorization task).
- You must NEVER fabricate, invent, or add job duties, requirements, skills, or qualifications
  that are not present in the source text. cleaned_description must only ever be a cleaned-up
  (de-cluttered) version of what's actually there -- never embellished or expanded.
- Strip forwarding artifacts: sender names, timestamps, emojis, "apply now"/"share this" links,
  forwarded-message banners, group-chat noise -- but preserve every substantive line about the
  role itself (responsibilities, requirements, qualifications, location, etc.).
- If the text does not appear to describe a job opening at all, set "title" to exactly
  "Unclear/Not a job posting" and put the raw text as-is into "cleaned_description".
- Respond with ONLY the JSON object. No markdown fences, no prose outside the JSON.
"""

    user_prompt = f"""Raw text to analyze:
\"\"\"
{raw_text}
\"\"\"

Extract the three fields now. Return valid JSON only."""

    message = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_response = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()

    match = re.search(r"\{.*\}", raw_response, re.DOTALL)
    if not match:
        raise ValueError(f"Could not locate JSON in extraction LLM response:\n{raw_response}")

    data = json.loads(match.group(0))
    return ExtractedJobFields(**data)


def _extract_with_fallback(raw_text: str) -> ExtractedJobFields:
    """
    Deterministic, dependency-free extraction used when no ANTHROPIC_API_KEY
    is set or the LLM call fails. Never crashes, never fabricates -- it is
    intentionally "dumb": first non-empty line becomes the title guess (if
    short enough to plausibly be a title), everything else becomes the
    description verbatim (minus a light noise-strip pass).
    """
    text = raw_text.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Light noise-strip: drop lines that are pure emoji/links/forwarding boilerplate.
    noise_patterns = [
        r"^forwarded",
        r"^שותף מ",
        r"^https?://\S+$",
        r"^\W+$",  # pure punctuation/emoji lines
    ]
    cleaned_lines = [
        ln for ln in lines
        if not any(re.match(p, ln, re.IGNORECASE) for p in noise_patterns)
    ]
    if not cleaned_lines:
        cleaned_lines = lines  # never end up with nothing

    # Heuristic title guess: first line, only if short and title-like.
    title_guess = "Untitled Position"
    description_lines = cleaned_lines
    if cleaned_lines and len(cleaned_lines[0]) <= 80:
        title_guess = cleaned_lines[0]
        description_lines = cleaned_lines[1:] or cleaned_lines

    cleaned_description = "\n".join(description_lines).strip() or text

    return ExtractedJobFields(
        title=title_guess,
        company_name=UNKNOWN_COMPANY,
        cleaned_description=cleaned_description,
    )


def extract_job_fields_from_raw_text(raw_text: str) -> tuple[ExtractedJobFields, str]:
    """
    Main entry point. Returns (ExtractedJobFields, mode) where mode is one of:
      - "llm"       : Anthropic extraction succeeded
      - "fallback"  : no API key set, used deterministic fallback
      - "fallback (after LLM error)" : LLM call was attempted but failed

    Never raises for LLM-availability reasons -- always degrades gracefully,
    matching the established pattern from tailor_skills.py.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("raw_text must not be blank")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _extract_with_fallback(raw_text), "fallback"

    try:
        return _extract_with_anthropic(raw_text), "llm"
    except Exception as e:
        logger.warning("[ingestion_service] LLM extraction failed (%s); using deterministic fallback.", e)
        return _extract_with_fallback(raw_text), "fallback (after LLM error)"
