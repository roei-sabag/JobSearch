"""
tailor_skills.py
-----------------
Core PoC script for the Autonomous AI-Driven Job Search & Resume Tailoring System.

Pipeline:
 1. Load job description, skills pool, and HTML CV template.
 2. Ask an LLM (Anthropic Claude) to:
    a. Select/prioritize/categorize skills from the ground-truth pool that best
       match the JD.
    b. Generate a "soft_skills_line": exactly 3 comma-separated soft-skill
       phrases freely derived from the JD (not restricted to a fixed bank -
       new phrases are persisted to authenticity_pool.json for later manual
       review/curation).
    c. Generate a "seeking_line": a single sentence of the form "Seeking a
       {role} Student position, with strong interest in {domain1}, {domain2}
       [, or {domain3}]." where {role} is derived from the JD and the domains
       come from the authentic seeking-domains bank.
    Falls back to a local deterministic keyword-overlap ranker + template-based
    generator if ANTHROPIC_API_KEY is not set or the LLM call fails.
 3. Enforce strict layout-safety guardrails (max categories / items / char length)
    so the rendered skills block never breaks the fixed CV layout, AND an
    authenticity guardrail so hard skills/technologies are never invented.
 4. Render the final HTML via Jinja2 and convert to PDF via Playwright.
 5. Track dynamic-field state across runs (cv_dynamic_state.json) so the
    tailoring report always shows exactly what changed vs. the previous run.
 6. Write a markdown rationale report explaining what was prioritized/omitted
    and what changed since the last run.

Usage:
    python tailor_skills.py

NOTE (Phase 3): the reusable pipeline functions in this module (tailor_with_anthropic,
tailor_with_fallback, validate_authenticity, select_relevant_courses,
validate_courses_authenticity, render_pdf) are imported and reused by
services/tailor_service.py for the DB-backed/backend-triggered tailoring flow,
rather than being duplicated there. render_pdf() accepts optional path overrides
specifically so it can target a job-specific output path without needing a
separate implementation.
"""

import os
import re
import json
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from jinja2 import Environment, FileSystemLoader

# --------------------------------------------------------------------------- #
# Configuration / Layout-safety guardrails
# --------------------------------------------------------------------------- #

WORKDIR = Path(__file__).resolve().parent
JD_PATH = WORKDIR / "sample_jd.txt"
SKILLS_POOL_PATH = WORKDIR / "skills_pool.json"
COURSES_POOL_PATH = WORKDIR / "courses_pool.json"
AUTHENTICITY_POOL_PATH = WORKDIR / "authenticity_pool.json"
TEMPLATE_PATH = WORKDIR / "cv_template.html"
OUTPUT_PDF_PATH = WORKDIR / "tailored_cv.pdf"
OUTPUT_REPORT_PATH = WORKDIR / "tailoring_report.md"
STATE_PATH = WORKDIR / "cv_dynamic_state.json"

MAX_CATEGORIES = 3          # never exceed original number of skill categories
MAX_SKILLS_PER_CATEGORY = 8  # protects line-wrap / vertical space
MAX_CATEGORY_LINE_CHARS = 130  # approx chars incl. label before layout risk

# --- Header dynamic-field layout guardrails (v2 header architecture) ---
#
# The CV header now renders, in this exact order:
#   Roei Sabag
#   +972-... | ... | LinkedIn
#   Electrical & Computer Engineering Student (2 semesters remaining)
#   {soft_skills_line}      <- exactly 3 comma-separated soft-skill phrases,
#                              freely LLM-generated per JD (grown into a pool
#                              for manual review), NOT restricted to a fixed
#                              bank - soft/character traits carry near-zero
#                              interview-failure risk if slightly embellished
#   {seeking_line}          <- "Seeking a {role} Student position, with
#                              strong interest in {domain}..." - the target
#                              role is woven directly into this ONE sentence,
#                              never rendered as a separate line/field
#                              anywhere else in the CV
MAX_SOFT_SKILLS_LINE_CHARS = 180   # 3 comma-separated phrases, layout-safe
SOFT_SKILLS_PHRASE_COUNT = 3       # TARGET phrase count for auto-generation (LLM/fallback)
SOFT_SKILLS_PHRASE_MIN = 1         # guardrail floor - allows manual finalize-selection flexibility
SOFT_SKILLS_PHRASE_MAX = 5         # guardrail ceiling - allows manual finalize-selection flexibility
MAX_SEEKING_LINE_CHARS = 280       # seeking_line now also carries the role phrase

# --- Header title-line dynamic fields (per explicit instruction) ---
#
# The "Electrical & Computer Engineering Student (2 semesters remaining)"
# line is now dynamic/configurable per tailoring run (human-in-the-loop,
# mirrors the coursework/soft-skills/domains pickers): the user can pick
# how many semesters remain (0-3, where 0 omits the parenthetical entirely
# per explicit instruction) and whether the word "Student" appears at all
# in the title line. Similarly, the seeking_line's role qualifier
# ("...Student position..." vs "...Junior position...") is now selectable.
DEFAULT_TITLE_ROLE = "Electrical & Computer Engineering"
DEFAULT_SEMESTERS_REMAINING = 2
DEFAULT_INCLUDE_STUDENT_IN_TITLE = True
DEFAULT_ROLE_QUALIFIER = "Student"
ALLOWED_ROLE_QUALIFIERS = ["Student", "Junior"]
MAX_SEMESTERS_REMAINING = 3
MIN_SEMESTERS_REMAINING = 0



# Guardrail: phrases that would imply fabricated professional
# experience/seniority a student candidate does not have. If the LLM's
# seeking_line/soft_skills_line contains any of these
# (case-insensitive), it is treated as an authenticity violation (schema
# validation fails -> falls back to the deterministic path), exactly like a
# fabricated hard skill would be.
FORBIDDEN_EXPERIENCE_CLAIM_PHRASES = [
    "years of experience", "years experience", "senior ", "expert in",
    "led a team", "managed a team", "led the team", "managed the team",
    "years in industry", "extensive experience", "proven track record",
    "professional experience in", "worked as a", "previously worked",
]

# Guardrail: words that read as genuine soft-skill/character language but are
# ALSO a real, specific technical tool name the candidate doesn't have on his
# ground-truth skills pool (e.g. "Excel" reads as "strive to succeed" in a
# phrase like "Strong Motivation to Excel", but could just as easily be
# misread as literally claiming Microsoft Excel proficiency - a fabricated
# hard skill). Checked case-insensitively as a whole word in soft_skills_line
# and seeking_line, so genuinely ambiguous phrasing is rejected outright
# rather than risk an interviewer reading it as a fabricated technical claim.
# NOTE (bug fix, root cause of "seeking_line/soft_skills_line never changes
# for JDs that mention 'excel'"): the previous version of this guardrail
# unconditionally rejected ANY occurrence of "excel"/"excels"/etc. as a whole
# word, on the theory that it could be misread as a claim of Microsoft Excel
# proficiency. In practice this is a very common, totally unambiguous English
# verb ("strive to excel", "motivation to excel") that appears in many real
# JDs (e.g. NVIDIA's "Strong motivation to grow and excel") - and because
# tailor_with_anthropic() runs with temperature=0, this guardrail deterministically
# rejected the LLM's response on EVERY SINGLE run for that JD, silently and
# permanently falling back to the generic local ranker every time (looking
# like the tailoring pipeline was "stuck"/not JD-specific at all, when
# actually the true JD-aware LLM output was being thrown away every run).
# Fixed by only flagging genuinely ambiguous phrasing: "excel" as a bare verb
# ("to excel", "excels at", "excelling in") is fine; only flag it when
# immediately adjacent to a term that would make it read as the spreadsheet
# software (e.g. "Microsoft Excel", "Excel spreadsheet", "in Excel").
FORBIDDEN_AMBIGUOUS_TERMS: List[str] = []  # kept for backward-compat imports; no longer used for whole-word ban

_EXCEL_SOFTWARE_CONTEXT_RE = re.compile(
    r"\b(microsoft|ms)\s+excel\b|\bexcel\s+(spreadsheet|sheet|workbook|file|macro)\b|\bexcel\s+vba\b",
    re.IGNORECASE,
)


def _contains_forbidden_ambiguous_term(text: str) -> Optional[str]:
    """
    Returns a matched forbidden/ambiguous phrase if "excel" appears in a
    context that would genuinely read as a claim of Microsoft Excel software
    proficiency (e.g. "Microsoft Excel", "Excel spreadsheet") - NOT for the
    common, unambiguous verb usage ("strive to excel", "motivation to
    excel"). Returns None if no genuinely ambiguous usage is found.
    """
    match = _EXCEL_SOFTWARE_CONTEXT_RE.search(text)
    if match:
        return match.group(0)
    return None




# Disjoint keyword sets per authentic seeking-domain, used by BOTH the
# deterministic fallback seeking_line generator (tailor_with_fallback) AND
# the human-in-the-loop "Target Role & Domains" picker
# (get_domain_options_with_suggestions), so the picker's "suggested" flags
# always agree with what the fallback algorithm would actually pick.
# Extracted to module level (was previously a local dict inside
# tailor_with_fallback()) so both consumers share one single source of
# truth and never drift apart.
DOMAIN_KEYWORD_MAP = {
    "Hardware Verification": {"verification", "testbench", "verifying", "verify"},
    "DFT (Design for Test)": {"dft"},
    "RTL Design": {"rtl"},
    "FPGA Development": {"fpga", "vivado", "synthesis"},
    "Digital Design": {"digital"},
    "Communication Systems": {"communication", "communications"},
    "Embedded Systems": {"embedded"},
    "Signal Processing": {"signal", "processing", "dsp"},
    "Computer Architecture": {"architecture", "cpu", "microarchitecture"},
    "Micro-architecture": {"microarchitecture", "pipelining", "microarchitectural"},
    "Hardware Integration": {"integration", "bringup", "bring-up"},
    "Firmware Development": {"firmware", "bare-metal", "baremetal", "bootloader"},
    "AI-Assisted Engineering": {"ai", "llm", "machine", "learning", "artificial", "intelligence"},
    "AI Tooling & Automation": {"automation", "tooling", "pipelines", "pipeline"},
    "Chip Design": {"chip", "asic", "silicon"},
}


MIN_COURSE_GRADE = 80        # per instruction: only 80-100 counts as relevant

MAX_COURSE_GRADE = 100       # anything >=100 that isn't a real grade (e.g. 400 binary-pass code) is excluded
MAX_RELEVANT_COURSES = 4     # layout guardrail: cap how many courses can be listed
MAX_COURSEWORK_LINE_CHARS = 150  # layout guardrail for the whole "Relevant Coursework: ..." line
COURSE_RELEVANCE_MIN_SCORE = 1   # a course must match at least 1 JD keyword/tag to be considered "very relevant"


# NOTE (bug fix, root cause of "seeking_line never changes between JDs"):
# this constant previously pointed to "claude-3-5-sonnet-20241022", a model
# ID that Anthropic has since retired (confirmed via a live 404
# not_found_error at runtime: "model: claude-3-5-sonnet-20241022"). Because
# tailor_with_anthropic() always raised on that call, EVERY tailoring run
# silently fell through to tailor_with_fallback() (mode logged as
# "local-fallback-keyword-overlap (after LLM error)" in cv_tailored rows -
# confirmed in the DB). The local fallback's domain-matching is a small,
# static keyword-overlap heuristic, so it kept picking nearly the same 1-2
# "seeking_line" domains for almost any hardware/verification JD, making it
# LOOK like the dynamic text wasn't being tailored at all. Using a live,
# valid model ID (same one already proven working in visual_qa_loop.py)
# restores the real per-JD LLM tailoring path.
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"


# The only authentic domains Roei can credibly "seek" a position in, based on
# his real background (ground truth, not to be fabricated). The LLM must pick
# a subset of these that best match the JD, never invent unrelated domains.
# NOTE: "Micro-architecture" and "Hardware Integration" were added per
# explicit instruction - both are genuinely authentic given Roei's real
# C/Embedded Systems background (grounded in the ground-truth skills pool),
# and were previously missing from this bank, causing the LLM's otherwise
# accurate/authentic seeking_line phrasing for chip-design-style JDs (e.g.
# NVIDIA) to be silently rejected by the guardrail below.
AUTHENTIC_SEEKING_DOMAINS = [
    "Hardware Verification", "DFT (Design for Test)", "RTL Design",
    "FPGA Development", "Digital Design", "Communication Systems",
    "Embedded Systems", "Signal Processing", "Computer Architecture",
    "Micro-architecture", "Hardware Integration",
    "communication-related hardware roles",
    "Firmware Development", "AI-Assisted Engineering", "AI Tooling & Automation",
    # NOTE (bug fix, per explicit instruction): "Chip Design" added as its own
    # first-class authentic domain. Previously it only ever appeared as part
    # of the free-text {role} phrase in seeking_line (e.g. "Seeking a Chip
    # Design & Verification Student position..."), never as a selectable
    # "interest in ..." domain -- so if the {role} phrase for a given JD
    # happened to omit it (or the LLM path failed for any reason and fell
    # back to the local ranker, which never mentions it at all), "Chip
    # Design" could vanish from the CV entirely despite being a genuinely
    # authentic domain given Roei's RTL/ASIC-design background.
    "Chip Design",
]


# Historical/legacy authentic-traits bank. Kept ONLY as the seed/default for
# authenticity_pool.json and as a fallback-mode building block - the LLM path
# no longer restricts soft_skills_line to this list (see instruction: soft
# skills are now freely LLM-generated per JD and grown into the pool for
# later manual curation, not validated against a closed bank).
AUTHENTIC_TRAITS = [
    "Motivated", "Detail-oriented", "Analytical", "Proactive", "Diligent",
    "Collaborative", "Curious", "Persistent", "Methodical", "Self-driven",
]

# Growable bank of ATOMIC (single-concept) soft-skill phrases, each mapped to
# JD keywords for deterministic relevance scoring / match-percentage
# calculation. "Atomic" is the key design fix here: every phrase must express
# EXACTLY ONE trait idea - it must NEVER stack two distinct traits joined by
# "&" (e.g. the old "Detail-Oriented & Meticulous" secretly counted as 2
# traits inside what was supposed to be 1 of exactly 3 slots, so a "3-phrase"
# soft_skills_line could silently smuggle in 5 real trait concepts). This
# bank also powers the human-in-the-loop "Relevant Soft Skills" picker in the
# UI (mirrors the existing Relevant Coursework picker), each option annotated
# with a JD match_percentage.
SOFT_SKILL_BANK_DEFAULT = [
    {"phrase": "Strong Analytical Thinking", "keywords": ["analytical", "analysis", "logical", "reasoning"]},
    {"phrase": "Problem-Solving Mindset", "keywords": ["problem", "solving", "troubleshooting", "debug", "debugging"]},
    {"phrase": "Collaborative Team Player", "keywords": ["team", "collaborative", "collaboration", "cross-functional", "interpersonal", "communication"]},
    {"phrase": "Detail-Oriented", "keywords": ["detail", "detailed", "precision", "accurate", "thoroughness", "quality"]},
    {"phrase": "Meticulous Approach", "keywords": ["meticulous", "careful", "rigorous"]},
    {"phrase": "Proactive Self-Starter", "keywords": ["proactive", "initiative", "starter", "ownership", "drive"]},
    {"phrase": "Diligent Work Ethic", "keywords": ["diligent", "hardworking", "dedicated", "thorough"]},
    {"phrase": "Highly Motivated", "keywords": ["motivated", "passionate", "driven", "enthusiastic", "ambitious"]},
    {"phrase": "Curious Mindset", "keywords": ["curious", "exploring", "inquisitive"]},
    {"phrase": "Fast Learner", "keywords": ["learn", "learning", "quick", "adaptable", "eager"]},
    {"phrase": "Persistent", "keywords": ["persistent", "perseverance", "tenacious"]},
    {"phrase": "Resilient", "keywords": ["resilient", "challenging", "adaptable"]},
    {"phrase": "Methodical Approach", "keywords": ["methodical", "systematic", "structured", "process"]},
    {"phrase": "Well-Organized", "keywords": ["organized", "disciplined", "structured"]},
    {"phrase": "Autonomous Worker", "keywords": ["independent", "autonomous", "unsupervised"]},
    {"phrase": "Self-Driven", "keywords": ["self-motivated", "self-driven", "ownership"]},
]
SOFT_SKILL_BANK: List[dict] = [dict(x) for x in SOFT_SKILL_BANK_DEFAULT]


load_dotenv(WORKDIR / ".env")


def join_with_and(items: List[str]) -> str:
    """
    Joins a list of phrases WITHOUT the word "or" and WITHOUT an Oxford
    comma, e.g. ["A", "B", "C"] -> "A, B and C" ; ["A", "B"] -> "A and B" ;
    ["A"] -> "A". Used for soft_skills_line and the seeking_line domain list,
    per explicit instruction to eliminate "or" from these fields entirely.
    """
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _split_and_list(text: str) -> List[str]:
    """
    Splits a free-text list of the form "A, B and C" (or the legacy "A, B,
    or C") into ["A", "B", "C"]. Used both by the soft_skills_line phrase
    counter and the seeking_line domain extractor, so both fields share one
    consistent, "or"-free-aware splitting rule.
    """
    parts = re.split(r",\s*|\s+and\s+|\s+or\s+", text, flags=re.IGNORECASE)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def load_authenticity_pool() -> dict:
    """
    Loads the growable authenticity pool (authentic character/work-style
    traits, authentic seeking-domains, and the atomic soft-skill bank) from
    authenticity_pool.json. Falls back to the hardcoded defaults (and seeds
    any missing keys into the file) so this module never crashes on a fresh
    checkout AND so pre-existing pool files (from before soft_skill_bank
    existed) get seamlessly upgraded in place.
    """
    pool = load_json(AUTHENTICITY_POOL_PATH) if AUTHENTICITY_POOL_PATH.exists() else {}
    changed = False
    if "authentic_traits" not in pool:
        pool["authentic_traits"] = list(AUTHENTIC_TRAITS)
        changed = True
    if "authentic_seeking_domains" not in pool:
        pool["authentic_seeking_domains"] = list(AUTHENTIC_SEEKING_DOMAINS)
        changed = True
    else:
        # Merge in any newly-added default domains (e.g. Micro-architecture,
        # Hardware Integration) that aren't yet present in an existing pool
        # file, without discarding anything already grown into it.
        for d in AUTHENTIC_SEEKING_DOMAINS:
            if not any(d.lower() == existing.lower() for existing in pool["authentic_seeking_domains"]):
                pool["authentic_seeking_domains"].append(d)
                changed = True
    if "soft_skill_bank" not in pool:
        pool["soft_skill_bank"] = [dict(x) for x in SOFT_SKILL_BANK_DEFAULT]
        changed = True

    if changed:
        save_authenticity_pool(pool)
    return pool


def save_authenticity_pool(pool: dict) -> None:
    AUTHENTICITY_POOL_PATH.write_text(json.dumps(pool, indent=2), encoding="utf-8")


def sync_authenticity_globals_from_pool(pool: dict) -> None:
    """
    Mutates the module-level AUTHENTIC_TRAITS / AUTHENTIC_SEEKING_DOMAINS /
    SOFT_SKILL_BANK lists IN PLACE (rather than reassigning them) so every
    consumer that already holds a reference to these lists
    (validate_authenticity(), the LLM system prompt built in
    tailor_with_anthropic(), and any external module doing
    `import tailor_skills as ts; ts.AUTHENTIC_TRAITS`) immediately sees the
    latest grown pool for the remainder of the running process, with zero
    need for a restart.
    """
    AUTHENTIC_TRAITS[:] = pool.get("authentic_traits", AUTHENTIC_TRAITS)
    AUTHENTIC_SEEKING_DOMAINS[:] = pool.get("authentic_seeking_domains", AUTHENTIC_SEEKING_DOMAINS)
    SOFT_SKILL_BANK[:] = pool.get("soft_skill_bank", SOFT_SKILL_BANK_DEFAULT)



def _extract_candidate_traits(soft_skills_line: str) -> List[str]:
    """
    Best-effort split of a free-text soft_skills_line into ATOMIC trait
    phrases for the pool-growth step, e.g.
    "Strong Analytical Thinking, Collaborative Team Player and Detail-
    Oriented" -> ["Strong Analytical Thinking", "Collaborative Team Player",
    "Detail-Oriented"].

    IMPORTANT (bug fix): this now splits on "and"/"or" too, not just commas
    -- the final phrase in the new "A, B and C" join format is separated
    from the rest by "and", not a comma, so a comma-only split previously
    left the last TWO real phrases fused together as one string (undercounting
    the true number of distinct trait concepts). Uses the shared
    _split_and_list() helper so soft_skills_line and seeking_line parsing
    stay consistent.
    """
    return [p[0].upper() + p[1:] for p in _split_and_list(soft_skills_line) if p]


def _extract_candidate_domains(seeking_line: str) -> List[str]:
    """
    Best-effort extraction of domain phrases from a seeking_line of the
    merged form "Seeking a {role} Student position, with strong interest
    in X, Y and Z." for the pool-growth step. Splits the "X, Y and Z" body
    (after "interest in") using the shared _split_and_list() helper (handles
    both the current "and"-joined format and any legacy "or"-joined text).
    """
    match = re.search(r"interest in (.+?)\.?\s*$", seeking_line.strip(), re.IGNORECASE)
    if not match:
        return []
    return _split_and_list(match.group(1))



def grow_authenticity_pool(response_dict: dict) -> dict:
    """
    Inspects a raw (pre-validation) tailoring response dict's soft_skills_line
    and seeking_line for any character traits / seeking-domains that are
    NOT yet present in the authenticity pool (case-insensitive substring
    match against existing entries). Any genuinely new trait/domain gets
    appended to authenticity_pool.json (persisted immediately) and the
    in-memory AUTHENTIC_TRAITS / AUTHENTIC_SEEKING_DOMAINS globals are
    synced.

    IMPORTANT (per explicit instruction): soft_skills_line is now a FULLY
    OPEN, freely-LLM-generated field -- it is NEVER validated/rejected
    against a fixed bank. This growth step exists purely so every new
    trait phrase the LLM surfaces gets persisted to authenticity_pool.json
    for Roei's later manual review/curation, NOT as a gate that blocks
    anything.

    SCOPE: this growth mechanism applies ONLY to character-style traits and
    seeking-domain phrases (soft, subjective self-description text). It
    NEVER touches skills_pool.json (technical skills) or courses_pool.json
    (real academic grades) -- those remain strictly closed ground-truth
    pools where fabrication risk must stay at zero.
    """
    pool = load_authenticity_pool()
    traits = pool.setdefault("authentic_traits", [])
    domains = pool.setdefault("authentic_seeking_domains", [])
    changed = False

    soft_skills_line = response_dict.get("soft_skills_line", "") or ""
    for candidate in _extract_candidate_traits(soft_skills_line):
        if candidate.lower() in {"and", "with", "a"}:
            continue
        already_known = any(
            candidate.lower() == t.lower() or candidate.lower() in t.lower() or t.lower() in candidate.lower()
            for t in traits
        )
        if not already_known:
            traits.append(candidate)
            changed = True

    seeking_line = response_dict.get("seeking_line", "") or ""
    for candidate in _extract_candidate_domains(seeking_line):
        already_known = any(
            candidate.lower() == d.lower() or candidate.lower() in d.lower() or d.lower() in candidate.lower()
            for d in domains
        )
        if not already_known:
            domains.append(candidate)
            changed = True

    if changed:
        save_authenticity_pool(pool)

    sync_authenticity_globals_from_pool(pool)
    return pool



# --------------------------------------------------------------------------- #
# Pydantic schema for structured LLM output (the "data contract")

# --------------------------------------------------------------------------- #

class SkillCategory(BaseModel):
    name: str
    skills: List[str] = Field(..., max_length=MAX_SKILLS_PER_CATEGORY)

    @field_validator("skills")
    @classmethod
    def check_line_length(cls, v, info):
        joined = ", ".join(v)
        if len(joined) > MAX_CATEGORY_LINE_CHARS:
            raise ValueError(
                f"Category '{info.data.get('name')}' skills line too long "
                f"({len(joined)} chars > {MAX_CATEGORY_LINE_CHARS}). Layout guardrail violated."
            )
        return v


class TailoredSkillsResponse(BaseModel):
    categories: List[SkillCategory] = Field(..., max_length=MAX_CATEGORIES)

    soft_skills_line: str = Field(
        ...,
        description=(
            "Exactly 3 ATOMIC (single-concept) soft-skill/character-trait phrases, "
            "joined as 'Phrase 1, Phrase 2 and Phrase 3' (NO Oxford comma, NEVER the "
            "word 'or'), freely derived from this specific JD. Each phrase must "
            "express EXACTLY ONE trait idea - NEVER stack two distinct traits with "
            "'&' (e.g. 'Detail-Oriented & Meticulous' is WRONG - it secretly counts "
            "as 2 traits; use just 'Detail-Oriented' OR just 'Meticulous', not both). "
            "Good example: 'Strong Analytical Thinking, Collaborative Team Player and "
            "Detail-Oriented'. NOT restricted to a fixed bank - genuine soft-skill "
            "framing only, never a fabricated HARD engineering skill/technology."
        ),
    )
    seeking_line: str = Field(

        ...,
        description=(
            "Full sentence of the exact merged form: 'Seeking a {role} Student "
            "position, with strong interest in {domain1}, {domain2} and {domain3}.' "
            "(NO Oxford comma, NEVER the word 'or'). The target role is derived from "
            "this JD and woven directly into this one sentence - never rendered as a "
            "separate field/line elsewhere."
        ),
    )

    rationale: List[str] = Field(

        default_factory=list,
        description="Bullet-point explanations mapping selected skills/traits/domains to JD keywords.",
    )
    omitted: List[str] = Field(
        default_factory=list,
        description="Skills from the pool that were deliberately left out or de-prioritized, with reasons folded in as 'Skill - reason' strings.",
    )

    @field_validator("soft_skills_line")
    @classmethod
    def check_soft_skills_line(cls, v):
        if len(v) > MAX_SOFT_SKILLS_LINE_CHARS:
            raise ValueError(
                f"soft_skills_line too long ({len(v)} chars > {MAX_SOFT_SKILLS_LINE_CHARS}). Layout guardrail violated."
            )
        if " or " in f" {v.lower()} ":
            raise ValueError(
                f"soft_skills_line ('{v}') must not contain the word 'or' - use 'and'-joined phrasing instead."
            )
        # NOTE (bug fix): counts ATOMIC trait phrases using the shared
        # _split_and_list() helper (splits on comma AND "and"), not a naive
        # comma-only split - the final phrase in the mandated "A, B and C"
        # format is separated by "and", not a comma, so a comma-only count
        # would undercount by 1. Also explicitly rejects any phrase that
        # stacks two concepts with "&" (the original root-cause bug: e.g.
        # "Detail-Oriented & Meticulous" secretly smuggling in 2 trait
        # concepts inside what must be exactly 1 of the N phrase slots).
        phrases = _split_and_list(v)
        for p in phrases:
            if "&" in p:
                raise ValueError(
                    f"soft_skills_line phrase '{p}' stacks two distinct traits with '&' - each "
                    f"phrase must express exactly ONE trait idea. Split into separate phrases instead."
                )
        if not (SOFT_SKILLS_PHRASE_MIN <= len(phrases) <= SOFT_SKILLS_PHRASE_MAX):
            raise ValueError(
                f"soft_skills_line must contain between {SOFT_SKILLS_PHRASE_MIN} and "
                f"{SOFT_SKILLS_PHRASE_MAX} atomic, comma/and-separated phrases, found {len(phrases)}: '{v}'."
            )
        v_lower = v.lower()
        for phrase in FORBIDDEN_EXPERIENCE_CLAIM_PHRASES:
            if phrase in v_lower:
                raise ValueError(
                    f"soft_skills_line ('{v}') contains forbidden fabricated-experience phrase: '{phrase}'."
                )
        ambiguous = _contains_forbidden_ambiguous_term(v)
        if ambiguous:
            raise ValueError(
                f"soft_skills_line ('{v}') contains ambiguous term '{ambiguous}' which could be misread "
                f"as a fabricated hard-skill/technology claim rather than genuine soft-skill phrasing."
            )
        return v


    @field_validator("seeking_line")
    @classmethod
    def check_seeking_line_length(cls, v):

        if len(v) > MAX_SEEKING_LINE_CHARS:
            raise ValueError(
                f"seeking_line too long ({len(v)} chars > {MAX_SEEKING_LINE_CHARS}). Layout guardrail violated."
            )
        return v

    @field_validator("seeking_line")
    @classmethod
    def check_seeking_line_no_or(cls, v):
        if " or " in f" {v.lower()} ":
            raise ValueError(
                f"seeking_line ('{v}') must not contain the word 'or' - use 'and'-joined domain phrasing instead."
            )
        return v

    @field_validator("seeking_line")
    @classmethod
    def check_seeking_line_domains_are_authentic(cls, v):
        """
        Schema-level authenticity guardrail (stronger/earlier check than the
        post-hoc validate_authenticity() pass): EVERY domain phrase appearing
        in the "with strong interest in ..." clause of seeking_line must
        match an entry in AUTHENTIC_SEEKING_DOMAINS. This fails fast right at
        LLM-response parsing time if the model invents a domain outside the
        ground-truth bank, rather than only catching it later in
        validate_authenticity().

        NOTE (bug fix, per explicit instruction): uses PARTIAL/substring
        matching in BOTH directions (not exact string equality), so an
        authentic short-form domain phrase like "Firmware" is correctly
        recognized as authentic even though the bank entry is the longer
        "Firmware Development" -- the earlier bug rejected genuinely
        authentic domains (e.g. "Firmware", "Micro-architecture", "Hardware
        Integration") purely because they didn't match a bank entry
        word-for-word.
        """
        domains_in_line = _extract_candidate_domains(v)
        if not domains_in_line:
            # Fall back to a whole-line substring check if extraction failed
            # (e.g. unexpected phrasing) rather than blocking on a parsing gap.
            v_lower = v.lower()
            if not any(domain.lower() in v_lower for domain in AUTHENTIC_SEEKING_DOMAINS):
                raise ValueError(
                    f"seeking_line ('{v}') does not contain any domain from the authentic bank: "
                    f"{AUTHENTIC_SEEKING_DOMAINS}. Fabricated/unlisted domain guardrail violated."
                )
            return v

        for candidate in domains_in_line:
            candidate_lower = candidate.lower()
            is_authentic = any(
                candidate_lower == d.lower()
                or candidate_lower in d.lower()
                or d.lower() in candidate_lower
                for d in AUTHENTIC_SEEKING_DOMAINS
            )
            if not is_authentic:
                raise ValueError(
                    f"seeking_line domain '{candidate}' is not present in (or a recognizable variant "
                    f"of) the authentic bank: {AUTHENTIC_SEEKING_DOMAINS}. Fabricated/unlisted domain "
                    f"guardrail violated."
                )
        return v

    @field_validator("seeking_line")
    @classmethod
    def check_seeking_line_has_student_framing(cls, v):
        """
        Guardrail for the merged target-role wording: the embedded role
        phrase must always be qualified by "Student" OR "Junior" (e.g.
        "Seeking a Chip Design & Verification Student position..." or
        "...Junior position...") so it can never imply the candidate
        already holds senior/unqualified professional seniority in that
        role. "Junior" was added (per explicit instruction) as a second
        allowed qualifier alongside "Student" for the human-in-the-loop
        finalize step -- both framings keep the same non-fabrication intent
        (never implying senior professional standing), so this guardrail is
        widened rather than weakened.
        """
        v_lower = v.lower()
        if "student" not in v_lower and "junior" not in v_lower:
            raise ValueError(
                f"seeking_line ('{v}') must include the word 'Student' or 'Junior' attached to the "
                f"role phrase, so it never implies existing senior professional seniority in that role."
            )
        for phrase in FORBIDDEN_EXPERIENCE_CLAIM_PHRASES:
            if phrase in v_lower:
                raise ValueError(
                    f"seeking_line ('{v}') contains forbidden fabricated-experience phrase: '{phrase}'."
                )
        ambiguous = _contains_forbidden_ambiguous_term(v)
        if ambiguous:
            raise ValueError(
                f"seeking_line ('{v}') contains ambiguous term '{ambiguous}' which could be misread "
                f"as a fabricated hard-skill/technology claim rather than genuine soft-skill phrasing."
            )
        return v



# --------------------------------------------------------------------------- #
# Helpers

# --------------------------------------------------------------------------- #

def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Lightweight morphological stemming (root-cause fix for exact-token-match
# keyword scoring silently missing genuine matches like "motivation" vs.
# "motivated", or "collaboration" vs. "collaborative")
# --------------------------------------------------------------------------- #

def _stem(word: str) -> str:
    """
    Very small, dependency-free suffix-stripping stemmer -- NOT a full
    linguistic stemmer (no external NLP library needed), but enough to
    collapse the common English derivational suffixes that keep causing
    genuine keyword matches to be silently missed by exact-string
    comparison (e.g. a JD saying "strong motivation" should match a
    keyword list containing "motivated", since both derive from the same
    root "motiv-"). Used EVERYWHERE keyword/tag overlap is scored (soft
    skills, seeking-domains, courses) so this fix is systemic rather than
    a one-off patch for a single word pair.

    Order matters: longer/more specific suffixes are stripped before
    shorter ones so e.g. "-ations" doesn't get mangled by a "-s" rule
    first. Falls back to the original word if stripping would leave an
    unreasonably short (<3 char) stem, to avoid over-aggressive collisions
    between unrelated short words.
    """
    w = word.lower()
    suffixes = [
        "ations", "ation", "izations", "ization",
        "ational", "ative", "atively",
        "ingly", "edly",
        "ities", "ity",
        "iveness", "iveness",
        "ing", "ers", "er", "ed", "es", "s",
    ]
    for suf in suffixes:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _stem_set(words) -> set:
    """Stems every word in an iterable, returning a set of stems."""
    return {_stem(w) for w in words}


def _stems_match(stem_a: str, stem_b: str) -> bool:
    """
    Compares two already-stemmed words for a match. Uses a PREFIX
    relationship (one stem starting with the other), not strict equality,
    because this lightweight suffix-stripping stemmer doesn't always reduce
    two genuinely-related words to the EXACT same string -- e.g.
    "motivation" -> "motiv" (via the "-ation" rule) but "motivated" ->
    "motivat" (via the much shorter "-ed" rule), so an exact-equality check
    would still miss this pair even after stemming. A minimum shared-prefix
    length of 4 avoids false positives between short, unrelated stems.
    """
    if not stem_a or not stem_b:
        return False
    if stem_a == stem_b:
        return True
    shorter, longer = (stem_a, stem_b) if len(stem_a) <= len(stem_b) else (stem_b, stem_a)
    return len(shorter) >= 4 and longer.startswith(shorter)


def _any_stem_matches(stems_a, stems_b) -> bool:
    """True if any stem in stems_a prefix-matches any stem in stems_b."""
    return any(_stems_match(a, b) for a in stems_a for b in stems_b)


def _keyword_overlap_score(item_keywords, jd_tokens) -> int:
    """
    Shared, stemming-aware keyword/JD-token overlap scorer. Counts how many
    of an item's own keywords have a stem that (prefix-)matches a stem
    among the JD's tokens (e.g. "motivation" vs. "motivated" both reduce to
    stems starting with "motiv"). Replaces the previous plain "set & set"
    exact-token intersection used across soft-skill scoring, seeking-domain
    scoring, and course scoring.
    """
    jd_stems = _stem_set(jd_tokens)
    score = 0
    for kw in item_keywords:
        if _any_stem_matches([_stem(kw.lower())], jd_stems):
            score += 1
    return score




def flatten_pool(pool: dict) -> List[str]:
    """Flat list of every allowed skill string, for authenticity validation."""
    flat = []
    for cat in pool["categories"]:
        flat.extend(cat["skills"])
    return flat


# Seed AUTHENTIC_TRAITS / AUTHENTIC_SEEKING_DOMAINS from authenticity_pool.json
# (or seed the file from the hardcoded defaults above, on first run) so the
# growable pool -- not just the hardcoded constants -- is what's actually
# used from here on. Placed here (after load_json() is defined above) since
# load_authenticity_pool() depends on it.
sync_authenticity_globals_from_pool(load_authenticity_pool())


def validate_authenticity(response: TailoredSkillsResponse, allowed_skills: List[str]) -> List[str]:
    """
    Guardrail: ensure every HARD skill returned by the LLM actually exists in
    the ground-truth pool. Returns a list of violation messages (empty ==
    pass).

    NOTE: soft_skills_line is intentionally NOT validated here against any
    fixed bank (per explicit instruction - soft skills are now freely
    LLM-generated per JD; new phrases are grown into authenticity_pool.json
    via grow_authenticity_pool() for later manual review, not gated).
    """
    allowed_set = {s.strip().lower() for s in allowed_skills}
    violations = []
    for cat in response.categories:
        for skill in cat.skills:
            if skill.strip().lower() not in allowed_set:
                violations.append(
                    f"Fabricated/unlisted skill detected: '{skill}' in category '{cat.name}' "
                    f"is NOT present in the ground-truth skills pool."
                )

    if not any(d.lower() in response.seeking_line.lower() for d in AUTHENTIC_SEEKING_DOMAINS):

        violations.append(
            f"Seeking line does not reference any authentic domain from the allowed bank: {AUTHENTIC_SEEKING_DOMAINS}"
        )

    return violations


# --------------------------------------------------------------------------- #
# LLM path (Anthropic Claude)
# --------------------------------------------------------------------------- #

def tailor_with_anthropic(jd_text: str, pool: dict) -> TailoredSkillsResponse:
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    allowed_skills_flat = flatten_pool(pool)

    system_prompt = f"""You are an expert technical resume editor. You must select, prioritize, and
categorize skills for a candidate's CV "Skills" section, based STRICTLY on a
ground-truth inventory of the candidate's real skills. You are NEVER allowed
to invent, add, or infer any HARD engineering skill/technology that is not
explicitly present in the provided skills pool. Fabrication of a hard skill
is a critical failure (the candidate could fail a technical interview over
it). You must ALSO generate three short dynamic text fields for the CV
header, described in detail below.

=== FIELD 1: "soft_skills_line" ===
Exactly {SOFT_SKILLS_PHRASE_COUNT} ATOMIC (single-concept) soft-skill /
character-trait phrases, each 2-4 words, joined together as
"Phrase 1, Phrase 2 and Phrase 3" (comma-separated, with the word "and"
before the FINAL phrase only - NO Oxford comma, and the word "or" must
NEVER appear anywhere in this field), e.g.:
  "Strong Analytical Thinking, Collaborative Team Player and Detail-Oriented"
CRITICAL RULE: each phrase must express EXACTLY ONE trait idea. NEVER join
two distinct traits with "&" inside a single phrase (e.g. "Detail-Oriented &
Meticulous" is WRONG, because it secretly packs 2 separate trait concepts
into what must be exactly 1 of the {SOFT_SKILLS_PHRASE_COUNT} slots - pick
only ONE of them instead). Analyze the JD's language for character/work-
style expectations (e.g. "self-starter", "thrives in ambiguity", "meticulous
attention to detail", "works well cross-functionally") and freely compose
the {SOFT_SKILLS_PHRASE_COUNT} atomic phrases that best reflect genuine,
authentic soft skills matching that signal. You are NOT restricted to any
fixed list here - use your judgment to phrase genuinely fitting soft
skills/traits for THIS specific JD. Do NOT invent hard technical claims in
this field (e.g. do not write "Expert in PCIe" here - that belongs nowhere,
since it would be a fabricated hard skill). Max {MAX_SOFT_SKILLS_LINE_CHARS}
characters total.


=== FIELD 2: "seeking_line" ===

A single sentence of the EXACT merged form:
  "Seeking a {{role}} Student position, with strong interest in {{domain1}}, {{domain2}} and {{domain3}}."
Where:
  - {{role}} is a short role phrase you derive directly from THIS JD's own
    title/focus (e.g. "Chip Design & Verification", "Hardware Verification"),
    ALWAYS immediately followed by the word "Student" (e.g. "...a Chip Design
    & Verification Student position...") so it never implies the candidate
    already holds that title professionally. Never use words like "Senior"
    or "Lead", never claim years of experience.
  - {{domain1}}, {{domain2}}, [{{domain3}}], [{{domain4}}] are normally 2-3,
    but may be exactly 4 when justified (see rule below), domains chosen
    ONLY from this authentic bank: {AUTHENTIC_SEEKING_DOMAINS}
    Pick whichever domains are most relevant to this JD's core technical
    focus. Join them with "and" only (comma-separated, "and" before the
    FINAL domain) - the word "or" must NEVER appear anywhere in this field.
    CRITICAL PRIORITIZATION RULE: give the HIGHEST priority to any domain
    from the bank whose name (or an obvious synonym of it) is EXPLICITLY
    named in the JD's own description of the actual day-to-day work/duties
    (e.g. a "What you'll be doing" / responsibilities section) - these are
    the strongest, most literal signals of what this specific role truly
    is about, and must not be crowded out by domains that only score well
    via generic keyword overlap (e.g. a degree-requirements line mentioning
    "Communication Engineering" is a much weaker signal than the
    responsibilities section explicitly listing "Firmware" or "Chip
    Design" as things the candidate will actually work on daily).
    4-DOMAIN RULE: if the JD's responsibilities section explicitly names 4
    OR MORE distinct duty-areas that each map to a different bank domain
    (e.g. "Micro-architecture, Firmware, Design/Verification and
    Integration" maps to 4 separate bank domains: Micro-architecture,
    Firmware Development, Hardware Verification/RTL Design, and Hardware
    Integration), you should include 4 domains (not just 3) in this field
    SO LONG AS the full seeking_line sentence still fits within
    {MAX_SEEKING_LINE_CHARS} characters - do not silently drop an explicitly
    -named duty-area just to force the count down to 3. Only fall back to 3
    (or fewer) domains if 4 genuinely don't fit the character budget, in
    which case keep the domains most explicitly/literally named over ones
    only weakly implied.
  - Max {MAX_SEEKING_LINE_CHARS} characters total.
  - This is the ONLY place the target role appears anywhere in the CV - it
    must NEVER be rendered as a separate standalone field/line/title.


Also produce:
- "rationale": bullet points explaining WHY each hard skill/soft-skill
  phrase/domain was chosen, each one quoting or referencing the specific JD
  phrase that justified it (auditability requirement).
- "omitted": skills from the pool that were deliberately de-prioritized or
  left out, with a short reason each.


HARD LAYOUT CONSTRAINTS (to preserve a fixed-layout PDF template):
- Return AT MOST {MAX_CATEGORIES} skill categories (use the same category names as the pool, or a subset).
- Each category must have AT MOST {MAX_SKILLS_PER_CATEGORY} skills.
- Each category's skills, when joined by ", ", must be AT MOST {MAX_CATEGORY_LINE_CHARS} characters.
- Order skills within each category by relevance to the job description (most relevant first).
- Order categories by overall relevance to the job description (most relevant first).
- soft_skills_line: exactly {SOFT_SKILLS_PHRASE_COUNT} comma-separated phrases, AT MOST {MAX_SOFT_SKILLS_LINE_CHARS} characters total.
- seeking_line: AT MOST {MAX_SEEKING_LINE_CHARS} characters, in the exact merged format described above.

Respond ONLY with a single valid JSON object matching this exact schema (no markdown, no prose outside JSON):
{{
  "categories": [
    {{"name": "<category name>", "skills": ["<skill>", "..."]}}
  ],
  "soft_skills_line": "<phrase 1>, <phrase 2> and <phrase 3>",
  "seeking_line": "Seeking a <role> Student position, with strong interest in <domain1>, <domain2> and <domain3>.",


  "rationale": ["<short bullet explaining why a skill/trait/domain was prioritized, referencing the JD keyword it maps to>", "..."],
  "omitted": ["<skill or category - short reason it was de-prioritized/omitted>", "..."]
}}

Ground-truth skills pool (the ONLY allowed hard skills):
{json.dumps(pool, indent=2)}
"""


    user_prompt = f"""Job Description:
\"\"\"
{jd_text}
\"\"\"

Select and prioritize the most relevant skills, soft_skills_line, and
seeking_line for this specific job description, following all constraints in the
system prompt. Return valid JSON only."""


    # temperature=0: makes the trait/domain-mapping reasoning as deterministic
    # and reproducible as possible run-to-run for the SAME JD (still genuinely
    # JD-specific across DIFFERENT JDs, since the mapping is driven by the
    # actual JD text) - important for an auditable hiring-facing pipeline.
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )


    raw_text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()

    # Extract JSON block defensively in case the model wraps it in markdown fences
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"Could not locate JSON in LLM response:\n{raw_text}")

    data = json.loads(match.group(0))

    # Grow the authenticity pool (traits/domains) BEFORE Pydantic validation,
    # so a genuinely-new-but-authentic domain the LLM surfaces for this JD is
    # added to authenticity_pool.json and merged into the in-memory
    # AUTHENTIC_SEEKING_DOMAINS bank first -- otherwise the seeking_line
    # schema validator below would reject it as "fabricated" purely because
    # it hadn't been seen yet. soft_skills_line phrases are also persisted
    # here purely for later manual review (never gated on).
    grow_authenticity_pool(data)

    response = TailoredSkillsResponse(**data)
    return response



# --------------------------------------------------------------------------- #
# Job Analysis (dedicated, unconstrained LLM call for the notification email)
# --------------------------------------------------------------------------- #

def analyze_job_posting(jd_text: str, candidate_background: str) -> dict:
    """
    Performs a SEPARATE, dedicated LLM call (not reused/combined with
    tailor_with_anthropic()'s CV-layout-constrained call) whose sole purpose
    is to produce a deep, unconstrained analysis of the job posting for the
    human-facing notification email. Deliberately kept independent of the
    CV-tailoring call because that call's prompt/response schema is bound by
    hard PDF layout limits (max words/chars) - an analysis worth reading
    needs the freedom to be as long/detailed as genuinely useful, which a
    second, purpose-built call provides at the cost of one extra API call
    (acceptable per explicit instruction: prioritize maximum accuracy/
    relevance over minimizing API cost).

    Returns a dict with keys: role_summary, must_have_requirements,
    nice_to_have_requirements, seniority_assessment, key_technologies,
    red_flags, fit_assessment, suggested_interview_questions. On any
    failure (no API key / API error / bad JSON), returns a dict with an
    "error" key instead of silently fabricating an analysis.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "No ANTHROPIC_API_KEY configured; job analysis unavailable."}

    try:
        import anthropic

        client = anthropic.Anthropic()

        system_prompt = f"""You are a senior technical career advisor and hiring-process expert.
Your job is to deeply analyze a job posting for a candidate and produce a
thorough, honest, and genuinely useful breakdown -- there is NO length limit,
prioritize accuracy, nuance, and usefulness over brevity.

Candidate's real technical background (ground truth -- use this ONLY to
assess fit, never invent additional candidate experience):
\"\"\"
{candidate_background}
\"\"\"

Respond ONLY with a single valid JSON object (no markdown, no prose outside
JSON) with EXACTLY these keys:
{{
  "role_summary": "<2-4 sentences: what this role actually involves day-to-day, in plain language>",
  "must_have_requirements": ["<explicit hard requirement from the JD>", "..."],
  "nice_to_have_requirements": ["<explicit 'nice to have'/preferred requirement from the JD>", "..."],
  "seniority_assessment": "<1-2 sentences: what seniority/experience level this posting is really targeting, and what that implies about expectations>",
  "key_technologies": ["<key technology/tool/skill the JD emphasizes>", "..."],
  "red_flags": ["<any vague language, unrealistic combination of requirements, potential scope-creep, or other genuine concern worth flagging -- empty list if genuinely none>"],
  "fit_assessment": "<honest 3-5 sentence assessment of how well the candidate's real background above matches this specific posting, including concrete strengths to lean into AND concrete gaps to be ready to address>",
  "suggested_interview_questions": ["<a smart, specific question the candidate could ask in an interview for THIS role that demonstrates genuine understanding of it>", "..."]
}}
"""

        user_prompt = f"""Job Description:
\"\"\"
{jd_text}
\"\"\"

Produce the full JSON analysis object as instructed in the system prompt."""

        message = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        ).strip()

        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            return {"error": f"Could not locate JSON in job-analysis LLM response:\n{raw_text[:500]}"}

        return json.loads(match.group(0))

    except Exception as e:
        return {"error": f"Job analysis LLM call failed: {e}"}


# --------------------------------------------------------------------------- #

# Fallback path: deterministic keyword-overlap ranker (no API key needed)

# --------------------------------------------------------------------------- #

def tailor_with_fallback(jd_text: str, pool: dict) -> TailoredSkillsResponse:
    jd_lower = jd_text.lower()

    # crude tokenization of JD for keyword overlap scoring
    jd_tokens = set(re.findall(r"[a-zA-Z+/#]+", jd_lower))

    # manual synonym expansion to catch common variations (helps fallback quality)
    synonym_map = {
        "systemverilog": {"verilog", "vhdl", "hardware", "description", "languages"},
        "rtl design": {"rtl", "digital", "systems"},
        "testbench development": {"testbenches", "testbench", "tests", "simulation"},
        "modelsim": {"simulation", "debugging"},
        "vivado": {"fpga"},
        "fpga synthesis": {"fpga", "synthesis"},
        "advanced computer architecture": {"architecture", "computer"},
        "python": {"python", "scripting", "programming"},
        "c/c++": {"c", "c++", "scripting", "programming"},
        "linux": {"linux"},
        "git": {"git"},
        "communication systems": {"communication", "communications"},
        "signal processing": {"signal", "processing"},
        "semiconductor devices": {"semiconductor", "devices"},
        "electrical circuits": {"electrical", "circuits"},
        "analog electronics": {"analog", "electronics"},
        "electromagnetic fields": {"electromagnetic", "fields"},
        "random processes": {"random", "processes"},
        "control theory": {"control", "theory"},
    }

    def score_skill(skill: str) -> int:
        skill_lower = skill.lower()
        score = 0
        # direct substring match
        if skill_lower in jd_lower:
            score += 5
        skill_words = set(re.findall(r"[a-zA-Z+/#]+", skill_lower))
        score += len(skill_words & jd_tokens)
        # synonym boost
        for key, syns in synonym_map.items():
            if key == skill_lower:
                score += len(syns & jd_tokens)
        return score

    scored_categories = []
    omitted = []

    for cat in pool["categories"]:
        scored_skills = sorted(
            cat["skills"], key=lambda s: score_skill(s), reverse=True
        )
        kept = []
        line = ""
        for s in scored_skills[:MAX_SKILLS_PER_CATEGORY]:
            candidate_line = ", ".join(kept + [s])
            if len(candidate_line) <= MAX_CATEGORY_LINE_CHARS:
                kept.append(s)
                line = candidate_line
            else:
                omitted.append(f"{s} - omitted from '{cat['name']}' to respect layout character budget")
        for s in scored_skills[MAX_SKILLS_PER_CATEGORY:]:
            omitted.append(f"{s} - lower relevance score, beyond max item count for '{cat['name']}'")

        cat_score = sum(score_skill(s) for s in kept)
        scored_categories.append((cat_score, {"name": cat["name"], "skills": kept}))

    scored_categories.sort(key=lambda x: x[0], reverse=True)
    categories = [c for _, c in scored_categories][:MAX_CATEGORIES]

    rationale = [
        f"'{c['name']}' kept (top skills: {', '.join(c['skills'][:3])}...) — "
        f"selected via keyword-overlap scoring against JD text (fallback mode, no LLM call)."
        for c in categories
    ]

    # --- Deterministic soft_skills_line selection (rule-based, N ATOMIC phrases) ---
    # NOTE (bug fix): every phrase here is now ATOMIC (exactly ONE trait
    # concept each) - the previous version stacked 2 distinct traits per
    # phrase with "&" (e.g. "Strong Analytical & Problem-Solving Mindset",
    # "Detail-Oriented & Meticulous"), which meant a "3-phrase" line
    # secretly smuggled in 5 real trait concepts. This map is built directly
    # from SOFT_SKILL_BANK (the same growable, atomic bank that also powers
    # the UI's human-in-the-loop Soft Skills picker + match-percentage
    # scoring), so the fallback path and the picker always stay in sync.
    # NOTE (bug fix, root cause of "strong motivation" never being
    # suggested/selected): uses the shared stemming-aware
    # _keyword_overlap_score() helper instead of a plain exact-token set
    # intersection, so a JD phrase like "strong motivation" correctly
    # matches the "Highly Motivated" bank entry's "motivated" keyword (both
    # share the stem "motiv"). This is the SAME scoring logic used by
    # get_soft_skill_options_with_suggestions() below, so the auto-selected
    # fallback phrases and the UI picker's "suggested" flags always agree.
    trait_scores = []
    for entry in SOFT_SKILL_BANK:
        score = _keyword_overlap_score(entry.get("keywords", []), jd_tokens)
        trait_scores.append((score, entry["phrase"]))
    trait_scores.sort(key=lambda x: x[0], reverse=True)
    top_phrases = [p for _, p in trait_scores[:SOFT_SKILLS_PHRASE_COUNT]]

    while len(top_phrases) < SOFT_SKILLS_PHRASE_COUNT:
        # pad deterministically with generically-true, unused phrases
        for _, p in trait_scores:
            if p not in top_phrases:
                top_phrases.append(p)
                break
        else:
            break
    soft_skills_line = join_with_and(top_phrases[:SOFT_SKILLS_PHRASE_COUNT])
    if len(soft_skills_line) > MAX_SOFT_SKILLS_LINE_CHARS:
        soft_skills_line = soft_skills_line[:MAX_SOFT_SKILLS_LINE_CHARS - 3].rstrip(", ") + "..."


    # --- Deterministic seeking_line selection (rule-based, merged role+domains) ---
    #
    # NOTE (bug fix, root cause of "Firmware" never appearing in seeking_line
    # even when the JD explicitly says 'Firmware'): this had THREE combined
    # defects, all fixed here:
    #   1. Domain keyword sets OVERLAPPED (e.g. "firmware" was a keyword for
    #      BOTH "Embedded Systems" and "Firmware Development"), so an
    #      explicit JD mention of "firmware" scored a tie between the two
    #      domains; "Embedded Systems" then won the tie purely because it
    #      appears earlier in dict iteration order -- Firmware Development
    #      was silently dropped despite being explicitly named in the JD.
    #      Fixed by giving every domain a disjoint keyword set.
    #   2. Only the top-2 domains were ever kept ([:2]), even though the
    #      seeking_line format supports 3 real domains. Fixed: now keeps up
    #      to 3 domains that genuinely scored > 0.
    #   3. "communication-related hardware roles" was unconditionally
    #      appended as a fixed 3rd slot regardless of relevance, permanently
    #      crowding out any genuinely-matched 3rd domain (like Firmware
    #      Development). Fixed: it is now only used as a LAST-RESORT filler
    #      when fewer than 3 real domains scored > 0, never displacing a
    #      real match.
    #   4. A domain whose name is explicitly/literally mentioned in the JD
    #      text now gets a strong scoring bonus, guaranteeing it outranks a
    #      same-stem inflection match from an unrelated domain.
    #
    # NOTE: domain_keyword_map used to be defined locally right here; it has
    # been extracted to the module-level DOMAIN_KEYWORD_MAP constant so the
    # human-in-the-loop "Target Role & Domains" picker
    # (get_domain_options_with_suggestions()) can share the exact same
    # scoring logic and never drift out of sync with this fallback path.
    domain_scores = _score_domains(jd_text)
    top_domains = [d for s, d in domain_scores if s > 0][:3]

    if not top_domains:
        top_domains = ["Hardware Verification", "RTL Design"]

    role_phrase = suggest_role_phrase(jd_text, top_domains)


    # Only pad with the generic filler phrase when there are genuinely fewer
    # than 3 real matched domains -- it must never displace a real match.
    domains_for_line = list(top_domains)
    if len(domains_for_line) < 3:
        domains_for_line.append("communication-related hardware roles")

    # NOTE (bug fix): "or" removed entirely per explicit instruction - domain
    # list is always joined with join_with_and() (comma-separated, "and"
    # before the final item, no Oxford comma, never the word "or").
    domain_phrase = join_with_and(domains_for_line)
    seeking_line = (
        f"Seeking a {role_phrase} Student position, with strong interest in {domain_phrase}."
    )
    if len(seeking_line) > MAX_SEEKING_LINE_CHARS:
        domain_phrase = join_with_and(top_domains[:2])
        seeking_line = (
            f"Seeking a {role_phrase} Student position, with strong interest in {domain_phrase}."
        )



    return TailoredSkillsResponse(
        categories=categories,
        soft_skills_line=soft_skills_line,
        seeking_line=seeking_line,
        rationale=rationale,
        omitted=omitted,
    )




# --------------------------------------------------------------------------- #
# Relevant Coursework selection (ground-truth transcript data, deterministic)
# --------------------------------------------------------------------------- #

def select_relevant_courses(jd_text: str, courses_pool: dict) -> List[dict]:
    """
    Selects courses from the ground-truth transcript pool that are:
      1. Graded strictly between MIN_COURSE_GRADE and MAX_COURSE_GRADE (80-100
         inclusive) — per instruction, grades >=100 that aren't real numeric
         grades (e.g. the '400' binary pass/fail code) or 'Pass'/'Exempt'/'F'
         entries are never included in the ground-truth pool to begin with,
         but this is enforced again here as a defense-in-depth guardrail.
      2. Relevant to the specific JD (keyword/tag overlap score above threshold).

    This selection is ALWAYS deterministic/rule-based (no LLM), since it deals
    with real academic grades — a domain where fabrication or misattribution
    risk must be zero.
    """
    jd_lower = jd_text.lower()
    # NOTE: tokenize on pure letter-runs only (no +/#/ etc.) so compound
    # substrings like "Python/C/C++" split into separate words ("python",
    # "c", "c") instead of becoming one giant unmatched token. This regex
    # must match the one used to tokenize tag words below, or tag-vs-JD
    # comparisons silently fail (a bug we hit and fixed: e.g. the "python"
    # and "c" tags on 'Data Structures' never matched "Python/C/C++" in the
    # JD when '+' and '/' were kept as word characters).
    jd_tokens = set(re.findall(r"[a-zA-Z]+", jd_lower))

    scored = []
    for course in courses_pool.get("courses", []):
        grade = course.get("grade")
        # Defense-in-depth grade guardrail (redundant with pool curation, but explicit)
        if grade is None or grade < MIN_COURSE_GRADE or grade >= MAX_COURSE_GRADE + 1:
            continue
        if grade > 100:
            continue

        # NOTE: scoring is based ONLY on curated 'tags' (not raw course-name words).
        # Course names often contain generic terms like "Electrical"/"Engineering"
        # that would coincidentally match almost any EE job description, causing
        # false positives (e.g. "Physics 1A" matching just because its title
        # contains "Electrical Engineering"). Tags are hand-curated to reflect
        # genuine subject-matter relevance, so only they are used for scoring.
        tags = set(t.lower() for t in course.get("tags", []))
        score = 0
        for tag in tags:
            tag_words = set(re.findall(r"[a-zA-Z]+", tag))
            if tag_words & jd_tokens:
                score += 1

        if score >= COURSE_RELEVANCE_MIN_SCORE:
            scored.append((score, course))



    scored.sort(key=lambda x: (-x[0], -x[1]["grade"]))
    selected = [c for _, c in scored][:MAX_RELEVANT_COURSES]

    # Layout guardrail: trim further if the joined line would be too long
    def line_len(courses):
        return len("Relevant Coursework: " + ", ".join(f"{c['name']} ({c['grade']})" for c in courses))

    while selected and line_len(selected) > MAX_COURSEWORK_LINE_CHARS:
        selected.pop()  # drop lowest-priority (last) course to fit layout

    return selected


def _keyword_match_percentage(item_keywords: List[str], jd_tokens: set) -> int:
    """
    Shared match-percentage calculation used by both the course picker and
    the soft-skill picker: what percentage of THIS item's own tags/keywords
    are actually mentioned somewhere in the JD's text. E.g. a course with 5
    tags where 3 appear in the JD scores 60%. An item with zero tags/keywords
    scores 0% (never divides by zero).

    NOTE (bug fix, root cause of "strong motivation" scoring 0% against a
    "motivated" keyword): matching is now STEM-AWARE via _stem()/_stem_set()
    instead of requiring an exact token match. A JD keyword like "motivation"
    and a bank keyword like "motivated" share the same stem ("motiv"), so
    they now correctly count as a match. This applies uniformly to every
    caller (courses AND soft skills), fixing the underlying issue for all
    present and future JD/keyword pairs, not just this one word.
    """
    item_keywords = [k.lower() for k in item_keywords]
    if not item_keywords:
        return 0
    jd_tokens_lower = {t.lower() for t in jd_tokens}
    jd_stems = _stem_set(jd_tokens_lower)
    matched = 0
    for kw in item_keywords:
        kw_words = set(re.findall(r"[a-zA-Z]+", kw))
        # Exact-token match (fast path) OR prefix-aware stem match (catches
        # inflection differences like motivation/motivated,
        # collaboration/collaborative, analysis/analytical, etc.)
        if kw_words & jd_tokens_lower:
            matched += 1
        elif _any_stem_matches(_stem_set(kw_words), jd_stems):
            matched += 1
    return round((matched / len(item_keywords)) * 100)




def get_course_options_with_suggestions(jd_text: str, courses_pool: dict) -> List[dict]:
    """
    Returns EVERY eligible course from the ground-truth pool (grade 80-100),
    each annotated with a "suggested" boolean flag reflecting whether the
    deterministic select_relevant_courses() algorithm would have picked it
    for this specific JD, AND a "match_percentage" (0-100) reflecting what
    fraction of the course's own tags actually appear in this JD's text.
    Used to power a human-in-the-loop UI where the user can review/override
    the algorithm's course suggestions rather than blindly trusting a
    keyword-tag heuristic that can occasionally surface a technically-tag-
    matching but not-truly-relevant course.
    """
    suggested = select_relevant_courses(jd_text, courses_pool)
    suggested_names = {c["name"] for c in suggested}
    jd_tokens = set(re.findall(r"[a-zA-Z]+", jd_text.lower()))

    options = []
    for course in courses_pool.get("courses", []):
        grade = course.get("grade")
        if grade is None or grade < MIN_COURSE_GRADE or grade > MAX_COURSE_GRADE:
            continue
        options.append({
            "name": course["name"],
            "grade": grade,
            "tags": course.get("tags", []),
            "suggested": course["name"] in suggested_names,
            "match_percentage": _keyword_match_percentage(course.get("tags", []), jd_tokens),
        })

    # Suggested-first ordering for a friendlier UI (algorithm's picks surface
    # at the top, but the user can still see and pick any other course).
    options.sort(key=lambda c: (not c["suggested"], -c["match_percentage"], -c["grade"]))
    return options


def get_soft_skill_options_with_suggestions(jd_text: str) -> List[dict]:
    """
    Returns EVERY entry in the growable SOFT_SKILL_BANK, each annotated with
    a "suggested" boolean flag (would the deterministic fallback ranker have
    picked this phrase among its top SOFT_SKILLS_PHRASE_COUNT for this JD)
    and a "match_percentage" (0-100, fraction of the phrase's own keywords
    that appear in the JD text). Mirrors get_course_options_with_suggestions()
    exactly, so the UI's "Relevant Soft Skills" picker behaves identically
    to the existing "Relevant Coursework" picker.
    """
    jd_tokens = set(re.findall(r"[a-zA-Z]+", jd_text.lower()))

    # NOTE (bug fix): uses the same stemming-aware _keyword_overlap_score()
    # as tailor_with_fallback()'s soft_skills_line selection, so the
    # "suggested" flags shown here always agree with what the deterministic
    # fallback path would actually pick (and correctly catches inflection
    # matches like "motivation" vs. "motivated").
    scored = []
    for entry in SOFT_SKILL_BANK:
        score = _keyword_overlap_score(entry.get("keywords", []), jd_tokens)
        scored.append((score, entry["phrase"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    suggested_phrases = {p for score, p in scored[:SOFT_SKILLS_PHRASE_COUNT] if score > 0}


    options = []
    for entry in SOFT_SKILL_BANK:
        options.append({
            "phrase": entry["phrase"],
            "suggested": entry["phrase"] in suggested_phrases,
            "match_percentage": _keyword_match_percentage(entry.get("keywords", []), jd_tokens),
        })

    options.sort(key=lambda o: (not o["suggested"], -o["match_percentage"]))
    return options



def get_skill_options_with_suggestions(jd_text: str, pool: dict, previously_selected: Optional[dict] = None) -> List[dict]:
    """
    Returns EVERY skill in the ground-truth skills_pool.json, grouped by
    category, each annotated with a "suggested" boolean flag and a
    "match_percentage" (0-100) reflecting keyword overlap with the JD text -
    mirrors get_course_options_with_suggestions() /
    get_soft_skill_options_with_suggestions() exactly, so the UI's new
    "Skills" picker behaves identically to the existing pickers. Powers the
    human-in-the-loop "choose which skills from your skill pool to include"
    feature.

    previously_selected (optional): dict of {category_name: [skill, ...]}
    from the most recent tailoring run (LLM/fallback selection) - if
    provided, a skill is marked "suggested" when it was actually included
    in that run's output (this is the TRUE algorithm suggestion, more
    accurate than re-scoring from scratch); otherwise falls back to a
    simple keyword-overlap heuristic against the JD text.
    """
    jd_lower = jd_text.lower()
    jd_tokens = set(re.findall(r"[a-zA-Z+/#]+", jd_lower))

    previously_selected = previously_selected or {}

    options = []
    for cat in pool.get("categories", []):
        cat_name = cat["name"]
        prev_selected_set = {s.lower() for s in previously_selected.get(cat_name, [])}
        for skill in cat.get("skills", []):
            skill_lower = skill.lower()
            skill_words = set(re.findall(r"[a-zA-Z+/#]+", skill_lower))
            match_percentage = 100 if skill_lower in jd_lower else round(
                (len(skill_words & jd_tokens) / max(len(skill_words), 1)) * 100
            )
            if prev_selected_set:
                suggested = skill_lower in prev_selected_set
            else:
                suggested = skill_lower in jd_lower or bool(skill_words & jd_tokens)
            options.append({
                "category": cat_name,
                "skill": skill,
                "suggested": suggested,
                "match_percentage": match_percentage,
            })
    return options


def validate_skill_selection_authenticity(selected_skills_by_category: dict, pool: dict) -> List[str]:
    """
    Guardrail: every user-selected skill (from the new Skills picker) must
    exist in the ground-truth skills_pool.json under the SAME category it
    was submitted under - mirrors validate_courses_authenticity()'s "never
    trust arbitrary client input" principle. Returns a list of violation
    messages (empty == pass).
    """
    violations = []
    ground_truth = {cat["name"]: set(s.lower() for s in cat["skills"]) for cat in pool.get("categories", [])}
    for cat_name, skills in selected_skills_by_category.items():
        if cat_name not in ground_truth:
            violations.append(f"Fabricated/unlisted category detected: '{cat_name}' not in ground-truth skills pool.")
            continue
        for skill in skills:
            if skill.lower() not in ground_truth[cat_name]:
                violations.append(
                    f"Fabricated/unlisted skill detected: '{skill}' in category '{cat_name}' is NOT "
                    f"present in the ground-truth skills pool."
                )
    return violations


def _score_domains(jd_text: str) -> List[tuple]:

    """
    Shared domain-scoring helper used by BOTH tailor_with_fallback()'s
    seeking_line generation AND get_domain_options_with_suggestions() (the
    human-in-the-loop Target Role & Domains picker), so the two never drift
    out of sync. Returns a list of (score, domain) tuples sorted by score
    descending, exactly the logic previously inlined in
    tailor_with_fallback().
    """
    jd_lower = jd_text.lower()
    jd_tokens = set(re.findall(r"[a-zA-Z+/#]+", jd_lower))
    domain_scores = []
    for domain, kws in DOMAIN_KEYWORD_MAP.items():
        score = _keyword_overlap_score(kws, jd_tokens)
        domain_stub = domain.split(" (")[0].lower()
        mention_count = jd_lower.count(domain_stub) + sum(jd_lower.count(kw) for kw in kws)
        score += 3 * mention_count
        domain_scores.append((score, domain))
    domain_scores.sort(key=lambda x: x[0], reverse=True)
    return domain_scores


def suggest_role_phrase(jd_text: str, top_domains: List[str], job_title: Optional[str] = None) -> str:
    """
    Suggests a role phrase for the "Seeking a {role} Student position..."
    sentence. Prefers an already-known job_title (e.g. from the Job DB row)
    if it's short/plausible enough to be a title; otherwise falls back to
    the JD's own first non-empty line (crude but effective heuristic, most
    postings lead with the job title); otherwise falls back to the
    top-scoring authentic domain. Extracted from tailor_with_fallback() so
    the human-in-the-loop picker (get_domain_options_with_suggestions) can
    surface the exact same suggestion the deterministic fallback would use.
    """
    if job_title and 0 < len(job_title.strip()) <= 60:
        return job_title.strip()
    first_line = next((l.strip() for l in jd_text.splitlines() if l.strip()), "")
    if 0 < len(first_line) <= 60:
        return first_line
    return top_domains[0] if top_domains else "Engineering"


def get_domain_options_with_suggestions(jd_text: str, job_title: Optional[str] = None) -> dict:
    """
    Returns EVERY entry in the authentic AUTHENTIC_SEEKING_DOMAINS bank, each
    annotated with a "suggested" boolean flag (would the deterministic
    fallback ranker have picked this domain among its top 3 for this JD) and
    a "match_percentage" (0-100, fraction of the domain's own keywords that
    appear in the JD text) -- mirrors get_course_options_with_suggestions()
    and get_soft_skill_options_with_suggestions() exactly, so the UI's new
    "Target Role & Domains" picker behaves identically to the existing
    Coursework/Soft-Skills pickers. Also returns a "suggested_role" string
    (the role phrase the deterministic algorithm would weave into
    seeking_line), which the UI pre-fills into an editable text box.
    """
    jd_tokens = set(re.findall(r"[a-zA-Z]+", jd_text.lower()))
    domain_scores = _score_domains(jd_text)
    top_domains = [d for s, d in domain_scores if s > 0][:3]
    suggested_set = set(top_domains)

    options = []
    for domain in AUTHENTIC_SEEKING_DOMAINS:
        kws = DOMAIN_KEYWORD_MAP.get(domain, set())
        options.append({
            "domain": domain,
            "suggested": domain in suggested_set,
            "match_percentage": _keyword_match_percentage(list(kws), jd_tokens) if kws else 0,
        })

    options.sort(key=lambda o: (not o["suggested"], -o["match_percentage"]))
    suggested_role = suggest_role_phrase(jd_text, top_domains, job_title=job_title)
    return {"suggested_role": suggested_role, "options": options}


def build_seeking_line(role_phrase: str, domains: List[str], role_qualifier: str = DEFAULT_ROLE_QUALIFIER) -> str:
    """
    Builds the mandated 'Seeking a {role} {qualifier} position, with strong
    interest in {domain1}, {domain2} and {domain3}.' sentence from an
    already-approved (human-in-the-loop) role phrase + domain list. Used by
    finalize_courses_for_job() when the user has manually edited/selected
    the role/domains via the new Target Role & Domains picker.

    role_qualifier (per explicit instruction) lets the human-in-the-loop
    finalize step pick "Student" (default) or "Junior" instead of always
    hardcoding "Student" - both are non-fabricating, entry-level framings.
    Falls back to the default qualifier if an unrecognized value is passed
    (never trusts arbitrary client input for this either).

    Truncates defensively (should rarely trigger given
    MAX_SEEKING_LINE_CHARS=280) to respect the layout guardrail even for
    unusually long human-edited role phrases.
    """
    qualifier = role_qualifier if role_qualifier in ALLOWED_ROLE_QUALIFIERS else DEFAULT_ROLE_QUALIFIER
    domain_phrase = join_with_and(domains)
    seeking_line = f"Seeking a {role_phrase} {qualifier} position, with strong interest in {domain_phrase}."
    if len(seeking_line) > MAX_SEEKING_LINE_CHARS:
        seeking_line = seeking_line[: MAX_SEEKING_LINE_CHARS - 3].rstrip(", ") + "..."
    return seeking_line


def build_title_line(
    semesters_remaining: int = DEFAULT_SEMESTERS_REMAINING,
    include_student: bool = DEFAULT_INCLUDE_STUDENT_IN_TITLE,
    title_role: str = DEFAULT_TITLE_ROLE,
) -> str:
    """
    Builds the CV header's title line (previously a hardcoded string):
    "Electrical & Computer Engineering Student (2 semesters remaining)".

    Per explicit instruction, this is now human-in-the-loop configurable:
      - semesters_remaining: 0, 1, 2, or 3. When 0, the parenthetical is
        OMITTED ENTIRELY (per explicit instruction: "0 semesters" means no
        parentheses at all, not "(0 semesters remaining)").
      - include_student: whether the word "Student" appears in the title
        line at all (e.g. for a JD where the user wants the line to simply
        read "Electrical & Computer Engineering" or "Electrical & Computer
        Engineering (1 semester remaining)").

    Guardrails: semesters_remaining is clamped to the allowed
    [MIN_SEMESTERS_REMAINING, MAX_SEMESTERS_REMAINING] range and title_role
    defaults to the real, ground-truth degree name -- this function never
    fabricates a different degree/major.
    """
    try:
        semesters_remaining = int(semesters_remaining)
    except (TypeError, ValueError):
        semesters_remaining = DEFAULT_SEMESTERS_REMAINING
    semesters_remaining = max(MIN_SEMESTERS_REMAINING, min(MAX_SEMESTERS_REMAINING, semesters_remaining))

    parts = [title_role]
    if include_student:
        parts.append("Student")
    title_line = " ".join(parts)

    if semesters_remaining > 0:
        unit = "semester" if semesters_remaining == 1 else "semesters"
        title_line += f" ({semesters_remaining} {unit} remaining)"

    return title_line



def validate_domains_authenticity(domains: List[str]) -> List[str]:
    """
    Guardrail: every user-selected domain (from the Target Role & Domains
    picker) must match (or be a recognizable partial variant of) an entry in
    AUTHENTIC_SEEKING_DOMAINS -- mirrors validate_courses_authenticity()'s
    "never trust arbitrary client input" principle. Returns a list of
    violation messages (empty == pass).
    """
    violations = []
    for d in domains:
        is_authentic = any(
            d.lower() == x.lower() or d.lower() in x.lower() or x.lower() in d.lower()
            for x in AUTHENTIC_SEEKING_DOMAINS
        )
        if not is_authentic:
            violations.append(
                f"Fabricated/unlisted domain detected: '{d}' is not present in (or a recognizable "
                f"variant of) the authentic seeking-domains bank."
            )
    return violations


def validate_courses_authenticity(selected_courses: List[dict], courses_pool: dict) -> List[str]:

    """Guardrail: every selected course + grade must exactly match the ground-truth pool."""

    violations = []
    ground_truth = {c["name"]: c["grade"] for c in courses_pool.get("courses", [])}
    for c in selected_courses:
        if c["name"] not in ground_truth:
            violations.append(f"Fabricated/unlisted course detected: '{c['name']}' not in ground-truth transcript pool.")
        elif ground_truth[c["name"]] != c["grade"]:
            violations.append(
                f"Grade mismatch for '{c['name']}': displayed {c['grade']} vs. ground-truth {ground_truth[c['name']]}."
            )
        if c["grade"] < MIN_COURSE_GRADE or c["grade"] > MAX_COURSE_GRADE:
            violations.append(f"Course '{c['name']}' grade {c['grade']} is outside the allowed 80-100 range.")
    return violations


# --------------------------------------------------------------------------- #
# Rendering: Jinja2 -> HTML -> Playwright -> PDF
# --------------------------------------------------------------------------- #

def render_pdf(
    response: TailoredSkillsResponse,
    relevant_courses: List[dict],
    output_pdf_path: Optional[Path] = None,
    rendered_html_path: Optional[Path] = None,
    template_path: Optional[Path] = None,
    title_line: Optional[str] = None,
):

    """
    Renders the Jinja2 template to HTML, then uses a headless Chromium
    browser (via Playwright) to print it to a pixel-accurate PDF.

    Optional path overrides let callers (e.g. services/tailor_service.py)
    render to a job-specific output path without duplicating this logic.
    When called with no optional args (as the standalone script does), the
    original default file locations are used unchanged.
    """
    from playwright.sync_api import sync_playwright

    output_pdf_path = output_pdf_path or OUTPUT_PDF_PATH
    rendered_html_path = rendered_html_path or (WORKDIR / "_rendered_cv.html")
    template_path = template_path or TEMPLATE_PATH
    template_dir = template_path.parent
    template_name = template_path.name

    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template(template_name)

    # CRITICAL FIX (asset-pathing isolation bug): cv_template.html previously
    # linked to cv_style.css via a relative <link href="cv_style.css">. When
    # rendered_html_path points at a different directory than the template
    # (e.g. output/cvs/_rendered_job_{id}.html, written by the FastAPI
    # background-task pipeline), Chromium/Playwright resolves that relative
    # href against the RENDERED HTML's directory, not the template's
    # directory -- so the stylesheet 404s silently and Chromium falls back
    # to its default fonts/layout, destroying the entire visual design while
    # the raw text still renders (hard to notice without a byte-level PDF
    # inspection). Permanent, path-independent fix: read the real CSS file's
    # content directly from beside the template (always correct, regardless
    # of the template's own location) and inline it into the HTML via
    # <style>{{ inline_css }}</style> in cv_template.html, so the rendered
    # HTML is fully self-contained and never depends on relative-path
    # resolution at render time.
    css_path = template_dir / "cv_style.css"
    inline_css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""

    html_out = template.render(
        tailored_skills=[c.model_dump() for c in response.categories],
        soft_skills_line=response.soft_skills_line,
        seeking_line=response.seeking_line,
        relevant_courses=relevant_courses,
        inline_css=inline_css,
        title_line=title_line or build_title_line(),
    )





    rendered_html_path.parent.mkdir(parents=True, exist_ok=True)
    rendered_html_path.write_text(html_out, encoding="utf-8")

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(rendered_html_path.resolve().as_uri())
        page.pdf(
            path=str(output_pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()


# --------------------------------------------------------------------------- #
# State tracking (what changed since last run)
# --------------------------------------------------------------------------- #

def load_previous_state() -> Optional[dict]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return None


def save_current_state(response: TailoredSkillsResponse, jd_text: str, relevant_courses: List[dict]):
    state = {
        "jd_snippet": jd_text[:200],
        "soft_skills_line": response.soft_skills_line,
        "seeking_line": response.seeking_line,

        "categories": [c.model_dump() for c in response.categories],
        "relevant_courses": relevant_courses,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def diff_against_previous(response: TailoredSkillsResponse, previous: Optional[dict], relevant_courses: List[dict]) -> List[str]:

    """Returns a list of human-readable change descriptions vs. the previous run."""
    changes = []
    if previous is None:
        changes.append("First run recorded — no previous state to compare against.")
        return changes

    if previous.get("soft_skills_line") != response.soft_skills_line:
        changes.append(
            f"**soft_skills_line** changed: \"{previous.get('soft_skills_line')}\" → \"{response.soft_skills_line}\""
        )
    else:
        changes.append(f"soft_skills_line unchanged: \"{response.soft_skills_line}\"")

    if previous.get("seeking_line") != response.seeking_line:

        changes.append(
            f"**seeking_line** changed: \"{previous.get('seeking_line')}\" → \"{response.seeking_line}\""
        )
    else:
        changes.append(f"seeking_line unchanged: \"{response.seeking_line}\"")

    prev_skills_flat = set()
    for c in previous.get("categories", []):
        prev_skills_flat.update(c.get("skills", []))
    new_skills_flat = set()
    for c in response.categories:
        new_skills_flat.update(c.skills)

    added = new_skills_flat - prev_skills_flat
    removed = prev_skills_flat - new_skills_flat
    if added:
        changes.append(f"**Skills added** vs. previous run: {', '.join(sorted(added))}")
    if removed:
        changes.append(f"**Skills removed** vs. previous run: {', '.join(sorted(removed))}")
    if not added and not removed:
        changes.append("Skills selection unchanged vs. previous run.")

    prev_courses = {c["name"] for c in previous.get("relevant_courses", [])}
    new_courses = {c["name"] for c in relevant_courses}
    courses_added = new_courses - prev_courses
    courses_removed = prev_courses - new_courses
    if courses_added:
        changes.append(f"**Relevant Coursework added** vs. previous run: {', '.join(sorted(courses_added))}")
    if courses_removed:
        changes.append(f"**Relevant Coursework removed** vs. previous run: {', '.join(sorted(courses_removed))}")
    if not courses_added and not courses_removed:
        changes.append("Relevant Coursework selection unchanged vs. previous run.")

    return changes



# --------------------------------------------------------------------------- #
# Report generation
# --------------------------------------------------------------------------- #

def write_report(response: TailoredSkillsResponse, mode: str, violations: List[str], changes: List[str],
                  relevant_courses: List[dict], course_violations: List[str]):
    lines = ["# Tailoring Report\n"]
    lines.append(f"**Mode used:** `{mode}`\n")

    lines.append("## What Changed vs. Previous Run\n")
    for c in changes:
        lines.append(f"- {c}")
    lines.append("")

    lines.append("## Dynamic Summary Fields\n")
    lines.append(f"- **soft_skills_line:** {response.soft_skills_line}")
    lines.append(f"- **seeking_line:** {response.seeking_line}")

    lines.append("")

    lines.append("## Relevant Coursework (from academic transcript, grade 80-100 only)\n")
    if relevant_courses:
        for c in relevant_courses:
            lines.append(f"- {c['name']} — Grade: {c['grade']}")
    else:
        lines.append("_No courses met both the relevance and grade (80-100) thresholds for this JD._")
    lines.append("")

    if violations or course_violations:
        lines.append("## ⚠️ Authenticity Guardrail Violations\n")
        for v in violations:
            lines.append(f"- {v}")
        for v in course_violations:
            lines.append(f"- {v}")
        lines.append("")


    lines.append("## Selected & Prioritized Skills\n")
    for cat in response.categories:
        lines.append(f"### {cat.name}")
        for s in cat.skills:
            lines.append(f"- {s}")
        lines.append("")

    lines.append("## Rationale (Mapping to Job Description Keywords)\n")
    if response.rationale:
        for r in response.rationale:
            lines.append(f"- {r}")
    else:
        lines.append("_No rationale returned._")
    lines.append("")

    lines.append("## Omitted / De-prioritized Skills\n")
    if response.omitted:
        for o in response.omitted:
            lines.append(f"- {o}")
    else:
        lines.append("_Nothing omitted._")
    lines.append("")

    OUTPUT_REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    jd_text = load_text(JD_PATH)
    pool = load_json(SKILLS_POOL_PATH)
    allowed_skills = flatten_pool(pool)
    courses_pool = load_json(COURSES_POOL_PATH)

    previous_state = load_previous_state()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    mode = "anthropic-claude-sonnet-4-5" if api_key else "local-fallback-keyword-overlap"

    print(f"[tailor_skills] Running in mode: {mode}")

    try:
        if api_key:
            response = tailor_with_anthropic(jd_text, pool)
        else:
            response = tailor_with_fallback(jd_text, pool)
    except Exception as e:
        print(f"[tailor_skills] LLM path failed ({e}); falling back to local ranker.")
        mode = "local-fallback-keyword-overlap (after LLM error)"
        response = tailor_with_fallback(jd_text, pool)

    violations = validate_authenticity(response, allowed_skills)
    if violations:
        print("[tailor_skills] AUTHENTICITY GUARDRAIL FAILED:")
        for v in violations:
            print(f"  - {v}")
        print("[tailor_skills] Stripping fabricated skills before rendering...")
        allowed_set = {s.strip().lower() for s in allowed_skills}
        for cat in response.categories:
            cat.skills = [s for s in cat.skills if s.strip().lower() in allowed_set]

    # Relevant Coursework — always deterministic/rule-based (never LLM-generated),
    # since it involves real academic grades where fabrication risk must be zero.
    relevant_courses = select_relevant_courses(jd_text, courses_pool)
    course_violations = validate_courses_authenticity(relevant_courses, courses_pool)
    if course_violations:
        print("[tailor_skills] COURSE AUTHENTICITY GUARDRAIL FAILED:")
        for v in course_violations:
            print(f"  - {v}")
        print("[tailor_skills] Dropping invalid courses before rendering...")
        ground_truth = {c["name"]: c["grade"] for c in courses_pool.get("courses", [])}
        relevant_courses = [
            c for c in relevant_courses
            if c["name"] in ground_truth and ground_truth[c["name"]] == c["grade"]
            and MIN_COURSE_GRADE <= c["grade"] <= MAX_COURSE_GRADE
        ]

    changes = diff_against_previous(response, previous_state, relevant_courses)

    render_pdf(response, relevant_courses)
    write_report(response, mode, violations, changes, relevant_courses, course_violations)
    save_current_state(response, jd_text, relevant_courses)

    print(f"[tailor_skills] Done. PDF written to: {OUTPUT_PDF_PATH}")
    print(f"[tailor_skills] Report written to: {OUTPUT_REPORT_PATH}")
    print(f"[tailor_skills] State saved to: {STATE_PATH}")



if __name__ == "__main__":
    main()
