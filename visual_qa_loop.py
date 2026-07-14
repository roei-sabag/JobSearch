"""
visual_qa_loop.py
------------------
Autonomous Visual QA & Auto-Correction Loop for the CV layout-preservation
engine (Phase 1.5).

Pipeline:
 1. Render `tailored_cv.pdf` from the current `cv_template.html` +
    `cv_style.css` (via Playwright/Chromium, same engine as tailor_skills.py).
 2. Rasterize page 1 of both the ORIGINAL `Roei_Sabag_CV.pdf` and the
    freshly generated `tailored_cv.pdf` to compressed JPEG images
    (via PyMuPDF -> Pillow) at a cost-optimized resolution.
 3. Send both images in a single multimodal message to a Vision LLM
    (Claude 3.5 Sonnet primary, GPT-4o fallback) acting as a strict UI/UX
    QA inspector. It returns a structured JSON verdict: a similarity score
    (0-100) plus a list of specific (selector, property, suggested_value)
    issues.
 4. Filter suggestions against the hard rules in `cv_rendering_rules.md`
    (no justify, no structural changes, etc.), then apply the surviving
    suggestions as targeted regex patches to `cv_style.css`.
 5. Re-render and re-evaluate. Repeat until similarity >= 95, iteration
    cap (5) reached, or score improvement plateaus (< 2 points).
 6. Write `qa_report.md` with the full convergence history.

Usage:
    python visual_qa_loop.py
"""

import os
import re
import json
import shutil
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

WORKDIR = Path(__file__).resolve().parent
ORIGINAL_PDF = WORKDIR / "Roei_Sabag_CV.pdf"
TEMPLATE_HTML = WORKDIR / "cv_template.html"
STYLE_CSS = WORKDIR / "cv_style.css"
TAILORED_PDF = WORKDIR / "tailored_cv.pdf"
RENDERED_HTML = WORKDIR / "_rendered_cv.html"
QA_REPORT_PATH = WORKDIR / "qa_report.md"
CSS_BACKUP_DIR = WORKDIR / "_css_history"

MAX_ITERATIONS = 8
TARGET_SIMILARITY = 90
PLATEAU_DELTA = 2
PLATEAU_STREAK_LIMIT = 2  # require this many CONSECUTIVE non-improving iterations before stopping


# Cost-optimization: moderate DPI + resized + JPEG compression instead of
# print-native 300 DPI PNG. This is a deliberate token/latency optimization
# for the *vision QA audit only* -- final PDF output still renders at full
# print quality via Playwright separately.
RASTER_DPI = 150
MAX_LONG_EDGE_PX = 1000
JPEG_QUALITY = 80

ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"
OPENAI_MODEL = "gpt-4o"

load_dotenv(WORKDIR / ".env")

# Selectors/properties that are NEVER allowed to be patched by the loop,
# enforced per cv_rendering_rules.md Section 4.
FORBIDDEN_PROPERTY_VALUES = {
    "text-align": {"justify"},  # never allow re-introducing justify
}
FORBIDDEN_SELECTOR_SUBSTRINGS = [
    "skills-block",  # structural container tied to Jinja2 loop - cosmetic only via allowed props
]
ALLOWED_PROPERTIES = {
    "font-size", "margin", "margin-top", "margin-bottom", "margin-left",
    "margin-right", "padding", "padding-top", "padding-bottom",
    "padding-left", "padding-right", "line-height", "color", "gap",
    "text-align", "letter-spacing", "font-weight", "border-bottom",
    "max-height", "overflow",
    # Added to allow restoring bullet-point styling on .skills-category
    # (matches original CV's bulleted skills list) without permitting any
    # structural/display changes to the Jinja2-driven .skills-block container.
    "list-style-type", "text-indent",
    # Additional safe, purely-cosmetic properties (no structural/display impact):
    "font-style", "text-decoration", "text-transform",
    # justify-content/align-items are permitted ONLY on already-flex elements
    # (.section-title, .entry-header already declare display:flex) to allow
    # tweaking two-point alignment without permitting `display` itself to change.
    "justify-content", "align-items",
}



# The Vision LLM must choose selectors ONLY from this whitelist (extracted
# from the real cv_template.html/cv_style.css) to prevent it from
# hallucinating selectors that don't exist in our actual template (e.g.
# ".name", ".profile", ".project-header .date" were invented in an early
# test run and silently no-op'd as dead CSS, or worse, got appended as
# duplicate/garbage rules).
KNOWN_SELECTORS = [
    "body", ".header", ".header h1", ".contact-line", ".title-line",
    ".summary", "hr.divider", ".section", ".section-title",
    ".section-title-text", ".section-title-date", ".entry",
    ".entry-header", ".entry-title", ".entry-date", ".entry-subtitle",
    "ul.bullets", "ul.bullets li", ".skills-block", ".skills-category",
    ".skills-category .cat-label",
]

# Basic CSS value validators per property, to reject hallucinated
# non-CSS values like "inline after title" or "#1a5490 or similar dark blue".
VALUE_VALIDATORS = {
    "text-align": re.compile(r"^(left|right|center|start|end)$"),
    "color": re.compile(r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|rgb\([^)]+\)|rgba\([^)]+\)|[a-zA-Z]+)$"),
    "font-weight": re.compile(r"^(\d{3}|normal|bold|bolder|lighter)$"),
    "overflow": re.compile(r"^(visible|hidden|scroll|auto|clip)$"),
}
GENERIC_LENGTH_RE = re.compile(r"^-?\d+(\.\d+)?(pt|px|cm|mm|em|rem)$")
GENERIC_MULTI_LENGTH_RE = re.compile(r"^(-?\d+(\.\d+)?(pt|px|cm|mm|em|rem)\s*){1,4}$")


# --------------------------------------------------------------------------- #
# Structured schema for Vision QA feedback
# --------------------------------------------------------------------------- #

class LayoutIssue(BaseModel):
    element: str
    css_selector_hint: str
    property: str
    current_value: Optional[str] = ""
    suggested_value: str
    reason: str


class VisualQAResult(BaseModel):
    similarity_score: int = Field(..., ge=0, le=100)
    issues: List[LayoutIssue] = Field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------- #
# Step 1: Render current template to PDF (Playwright/Chromium)
# --------------------------------------------------------------------------- #

def render_current_pdf():
    from jinja2 import Environment, FileSystemLoader
    from playwright.sync_api import sync_playwright

    # For this QA loop we render with a neutral/representative skills block
    # (re-use whatever tailored_skills content already exists in the last
    # _rendered_cv.html if present, else fall back to the raw skills pool
    # grouped as-is) so layout fidelity is judged on realistic content.
    skills_pool_path = WORKDIR / "skills_pool.json"
    if skills_pool_path.exists():
        pool = json.loads(skills_pool_path.read_text(encoding="utf-8"))
        tailored_skills = pool["categories"]
    else:
        tailored_skills = []

    # Same inline-CSS fix as tailor_skills.py's render_pdf(): the template no
    # longer links cv_style.css via a relative <link href>, it expects an
    # `inline_css` context var so the rendered HTML is fully self-contained
    # regardless of which directory it's written to.
    inline_css = STYLE_CSS.read_text(encoding="utf-8") if STYLE_CSS.exists() else ""

    env = Environment(loader=FileSystemLoader(str(WORKDIR)))
    template = env.get_template("cv_template.html")
    html_out = template.render(tailored_skills=tailored_skills, inline_css=inline_css)
    RENDERED_HTML.write_text(html_out, encoding="utf-8")


    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(RENDERED_HTML.resolve().as_uri())
        page.pdf(
            path=str(TAILORED_PDF),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()


# --------------------------------------------------------------------------- #
# Step 2: PDF page 1 -> compressed JPEG bytes (PyMuPDF + Pillow)
# --------------------------------------------------------------------------- #

def pdf_page_to_jpeg_bytes(pdf_path: Path, dpi: int = RASTER_DPI) -> bytes:
    import fitz  # PyMuPDF
    from PIL import Image
    import io

    doc = fitz.open(str(pdf_path))
    page = doc.load_page(0)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()

    # Resize to cap the long edge (token/cost optimization)
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > MAX_LONG_EDGE_PX:
        scale = MAX_LONG_EDGE_PX / long_edge
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Step 3: Vision LLM call (Anthropic primary, OpenAI fallback)
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are a rigorous UI/UX QA Inspector specializing in print/PDF document
layout fidelity. You will be shown two images:
  IMAGE 1: the ORIGINAL reference CV layout (ground truth).
  IMAGE 2: a GENERATED CV layout that should visually match the original as
           closely as possible (same content categories, but text may differ
           slightly - you must judge LAYOUT fidelity, not content wording).

Evaluate strictly on: margins/whitespace, text alignment (left vs justified vs
centered), font sizing/weight consistency, line-height/spacing, element
positioning (e.g. title-left/date-right rows), and any visual "stretching" or
unnatural gaps.

Do NOT penalize differences in the actual text content/wording - only layout,
spacing, and typography fidelity.

CRITICAL CONSTRAINT: You may ONLY reference CSS selectors from this exact
whitelist (these are the real selectors that exist in the template). Do NOT
invent, guess, or reference any other selector:
{known_selectors}

CRITICAL CONSTRAINT: All "suggested_value" fields must be valid, literal CSS
values only (e.g. "left", "#1a4fa0", "12pt", "8px", "600") - never prose,
never placeholders like "similar dark blue" or "inline after title", never
multiple options separated by "or". If you cannot express your suggestion as
a single valid CSS value, omit that issue entirely.

Respond ONLY with a single valid JSON object with this exact schema (no
markdown fences, no prose outside JSON):

{
  "similarity_score": <integer 0-100>,
  "issues": [
    {
      "element": "<short human description, e.g. 'summary paragraph'>",
      "css_selector_hint": "<best-guess CSS selector, e.g. '.summary'>",
      "property": "<CSS property name, e.g. 'text-align'>",
      "current_value": "<value you believe is currently applied, if inferable>",
      "suggested_value": "<value you recommend instead>",
      "reason": "<short justification tied to a specific visual difference>"
    }
  ],
  "summary": "<1-2 sentence overall verdict>"
}

Only report genuine, visually-evident layout discrepancies. If the two layouts
are already highly similar, return a high score and an empty or near-empty
issues list. Do not invent issues to pad the list.
"""


def _build_system_prompt() -> str:
    return SYSTEM_PROMPT.replace(
        "{known_selectors}",
        "\n".join(f"  - {s}" for s in KNOWN_SELECTORS),
    )


def call_anthropic_vision(original_jpeg: bytes, generated_jpeg: bytes) -> VisualQAResult:
    import anthropic
    import base64

    client = anthropic.Anthropic()
    o_b64 = base64.b64encode(original_jpeg).decode()
    g_b64 = base64.b64encode(generated_jpeg).decode()

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=_build_system_prompt(),
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "IMAGE 1 (ORIGINAL reference):"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": o_b64}},
                {"type": "text", "text": "IMAGE 2 (GENERATED, to be evaluated):"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": g_b64}},
                {"type": "text", "text": "Evaluate now and return the JSON verdict."},
            ],
        }],
    )
    raw_text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in Anthropic vision response:\n{raw_text}")
    return VisualQAResult(**json.loads(match.group(0)))


def call_openai_vision(original_jpeg: bytes, generated_jpeg: bytes) -> VisualQAResult:
    import openai
    import base64

    client = openai.OpenAI()
    o_b64 = base64.b64encode(original_jpeg).decode()
    g_b64 = base64.b64encode(generated_jpeg).decode()

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": [
                {"type": "text", "text": "IMAGE 1 (ORIGINAL reference):"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{o_b64}"}},
                {"type": "text", "text": "IMAGE 2 (GENERATED, to be evaluated):"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{g_b64}"}},
                {"type": "text", "text": "Evaluate now and return the JSON verdict."},
            ]},
        ],
        max_tokens=2000,
    )
    raw_text = response.choices[0].message.content.strip()
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in OpenAI vision response:\n{raw_text}")
    return VisualQAResult(**json.loads(match.group(0)))


def run_vision_qa(original_jpeg: bytes, generated_jpeg: bytes) -> tuple[VisualQAResult, str]:
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if anthropic_key:
        try:
            return call_anthropic_vision(original_jpeg, generated_jpeg), "anthropic-claude-3.5-sonnet"
        except Exception as e:
            print(f"[visual_qa_loop] Anthropic vision call failed ({e}); trying OpenAI fallback.")

    if openai_key:
        try:
            return call_openai_vision(original_jpeg, generated_jpeg), "openai-gpt-4o (fallback)"
        except Exception as e:
            print(f"[visual_qa_loop] OpenAI vision call failed ({e}).")

    raise RuntimeError(
        "No working Vision LLM provider available. Set ANTHROPIC_API_KEY and/or "
        "OPENAI_API_KEY in the environment or .env file."
    )


# --------------------------------------------------------------------------- #
# Step 4: Guardrail filtering + targeted CSS patching
# --------------------------------------------------------------------------- #

def _is_valid_css_value(prop: str, val: str) -> bool:
    """Reject hallucinated/non-CSS values (prose, placeholders, 'X or Y')."""
    if not val or " or " in val.lower():
        return False
    validator = VALUE_VALIDATORS.get(prop)
    if validator:
        return bool(validator.match(val))
    # Generic fallback for length-based properties (margin/padding/font-size/gap/etc.)
    if GENERIC_MULTI_LENGTH_RE.match(val):
        return True
    # A handful of properties accept keyword values we haven't explicitly validated;
    # be conservative and require it to look like a single CSS token (no spaces/prose).
    if re.match(r"^[a-zA-Z0-9#().,%\-]+$", val) and len(val) <= 40:
        return True
    return False


def filter_issues(issues: List[LayoutIssue]) -> tuple[List[LayoutIssue], List[str]]:
    """Apply cv_rendering_rules.md governance. Returns (allowed, rejected_reasons)."""
    allowed = []
    rejected = []
    known_selectors_lower = {s.lower() for s in KNOWN_SELECTORS}

    for issue in issues:
        prop = issue.property.strip().lower()
        val = issue.suggested_value.strip()
        val_lower = val.lower()
        selector = issue.css_selector_hint.strip()
        selector_lower = selector.lower()

        if selector_lower not in known_selectors_lower:
            rejected.append(f"Rejected selector '{selector}' - not in KNOWN_SELECTORS whitelist (likely hallucinated).")
            continue
        if prop not in ALLOWED_PROPERTIES:
            rejected.append(f"Rejected '{prop}' on '{selector}' - property not in allowed cosmetic list.")
            continue
        if prop in FORBIDDEN_PROPERTY_VALUES and val_lower in FORBIDDEN_PROPERTY_VALUES[prop]:
            rejected.append(f"Rejected '{prop}: {val}' on '{selector}' - forbidden value per cv_rendering_rules.md.")
            continue
        if any(bad in selector_lower for bad in FORBIDDEN_SELECTOR_SUBSTRINGS) and prop not in {"font-size", "max-height", "overflow"}:
            rejected.append(f"Rejected '{prop}' on '{selector}' - structural container, only font-size/max-height/overflow allowed.")
            continue
        if not _is_valid_css_value(prop, val):
            rejected.append(f"Rejected '{prop}: {val}' on '{selector}' - not a valid literal CSS value (likely hallucinated/prose).")
            continue

        allowed.append(issue)
    return allowed, rejected


def apply_css_patches(issues: List[LayoutIssue]) -> List[str]:
    """
    Targeted patch: for each issue, try to find `<selector> { ... <property>: <value>; ... }`
    and replace the property's value. If the selector/property combo doesn't
    exist yet, append a new rule block. Returns list of human-readable patch logs.
    """
    css_text = STYLE_CSS.read_text(encoding="utf-8")
    logs = []

    for issue in issues:
        selector = issue.css_selector_hint.strip()
        prop = issue.property.strip()
        new_val = issue.suggested_value.strip().rstrip(";")

        # Try to find the selector block
        block_pattern = re.compile(
            re.escape(selector) + r"\s*\{([^}]*)\}", re.DOTALL
        )
        block_match = block_pattern.search(css_text)

        if block_match:
            block_content = block_match.group(1)
            prop_pattern = re.compile(
                r"(" + re.escape(prop) + r"\s*:\s*)([^;]+)(;)"
            )
            if prop_pattern.search(block_content):
                new_block_content = prop_pattern.sub(
                    lambda m: f"{m.group(1)}{new_val}{m.group(3)}", block_content
                )
                logs.append(f"Patched existing '{prop}' in '{selector}' -> {new_val}")
            else:
                new_block_content = block_content.rstrip() + f"\n    {prop}: {new_val};\n"
                logs.append(f"Added new '{prop}: {new_val};' to existing '{selector}' block")

            start, end = block_match.span(1)
            css_text = css_text[:start] + new_block_content + css_text[end:]
        else:
            # Selector not found -- append a new rule block at end of file
            css_text += f"\n\n{selector} {{\n    {prop}: {new_val};\n}}\n"
            logs.append(f"Appended new rule block '{selector} {{ {prop}: {new_val}; }}'")

    STYLE_CSS.write_text(css_text, encoding="utf-8")
    return logs


def backup_css(iteration: int):
    CSS_BACKUP_DIR.mkdir(exist_ok=True)
    shutil.copy(STYLE_CSS, CSS_BACKUP_DIR / f"cv_style_iter{iteration}.css")


# --------------------------------------------------------------------------- #
# Main convergence loop
# --------------------------------------------------------------------------- #

def main():
    history = []  # list of dicts: iteration, score, provider, issues_applied, issues_rejected

    if not ORIGINAL_PDF.exists():
        raise FileNotFoundError(f"Original reference PDF not found: {ORIGINAL_PDF}")

    original_jpeg = pdf_page_to_jpeg_bytes(ORIGINAL_PDF)
    print(f"[visual_qa_loop] Original reference rasterized ({len(original_jpeg)} bytes JPEG).")

    prev_score = None
    plateau_streak = 0
    best_score = -1
    best_css_text = STYLE_CSS.read_text(encoding="utf-8")  # initial state as baseline "best"

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n[visual_qa_loop] === Iteration {iteration}/{MAX_ITERATIONS} ===")
        backup_css(iteration - 1)  # snapshot BEFORE this iteration's patch

        render_current_pdf()
        generated_jpeg = pdf_page_to_jpeg_bytes(TAILORED_PDF)
        print(f"[visual_qa_loop] Generated CV rasterized ({len(generated_jpeg)} bytes JPEG).")

        try:
            result, provider = run_vision_qa(original_jpeg, generated_jpeg)
        except Exception as e:
            print(f"[visual_qa_loop] Vision QA failed entirely: {e}")
            history.append({
                "iteration": iteration, "score": None, "provider": "none",
                "issues_applied": [], "issues_rejected": [], "error": str(e),
            })
            break

        print(f"[visual_qa_loop] Provider: {provider} | Similarity score: {result.similarity_score}")
        print(f"[visual_qa_loop] Summary: {result.summary}")

        # Track the best-scoring CSS state seen so far, BEFORE this iteration's
        # patch is applied. This protects against the Vision LLM's inherent
        # scoring noise (the same document can score differently between
        # calls) causing the loop to wander away from a good state and never
        # return - we always fall back to the best snapshot at the end.
        if result.similarity_score > best_score:
            best_score = result.similarity_score
            best_css_text = STYLE_CSS.read_text(encoding="utf-8")
            print(f"[visual_qa_loop] New best score: {best_score} (CSS snapshot saved).")

        allowed_issues, rejected_reasons = filter_issues(result.issues)
        for r in rejected_reasons:
            print(f"[visual_qa_loop] GUARDRAIL: {r}")

        patch_logs = []
        if allowed_issues:
            patch_logs = apply_css_patches(allowed_issues)
            for log in patch_logs:
                print(f"[visual_qa_loop] CSS PATCH: {log}")

        history.append({
            "iteration": iteration,
            "score": result.similarity_score,
            "provider": provider,
            "issues_applied": patch_logs,
            "issues_rejected": rejected_reasons,
            "vision_summary": result.summary,
        })

        # Stop conditions
        if result.similarity_score >= TARGET_SIMILARITY:
            print(f"[visual_qa_loop] Target similarity reached ({result.similarity_score} >= {TARGET_SIMILARITY}). Stopping.")
            break
        if not allowed_issues:
            print("[visual_qa_loop] No actionable (allowed) issues returned. Stopping (nothing left to patch).")
            break

        if prev_score is not None and (result.similarity_score - prev_score) < PLATEAU_DELTA:
            plateau_streak += 1
            print(f"[visual_qa_loop] No significant improvement this iteration "
                  f"({result.similarity_score - prev_score} < {PLATEAU_DELTA}). "
                  f"Plateau streak: {plateau_streak}/{PLATEAU_STREAK_LIMIT}.")
            if plateau_streak >= PLATEAU_STREAK_LIMIT:
                print(f"[visual_qa_loop] Plateau confirmed for {PLATEAU_STREAK_LIMIT} consecutive iterations. Stopping.")
                break
        else:
            plateau_streak = 0

        prev_score = result.similarity_score

    # Always finish on the best-scoring CSS state seen across all iterations,
    # not necessarily the last one (the Vision LLM's scoring is noisy enough
    # that later iterations can regress even with "improving" patches).
    print(f"\n[visual_qa_loop] Restoring best-scoring CSS state (score={best_score}) as final output.")
    STYLE_CSS.write_text(best_css_text, encoding="utf-8")
    render_current_pdf()

    write_qa_report(history, best_score)
    print(f"\n[visual_qa_loop] Done. Report written to: {QA_REPORT_PATH}")


def write_qa_report(history: list, best_score: int = None):
    lines = ["# Visual QA Convergence Report\n"]

    if best_score is not None:
        lines.append(f"**Final output uses the BEST-scoring CSS state found: {best_score}/100** "
                      f"(not necessarily the last iteration's state, to protect against Vision-LLM scoring noise).\n")

    lines.append("## Optimization Choices\n")
    lines.append(f"- PDF rasterization: PyMuPDF (`fitz`) at {RASTER_DPI} DPI, resized to max "
                  f"{MAX_LONG_EDGE_PX}px long edge, re-encoded as JPEG q={JPEG_QUALITY} "
                  f"(instead of print-native 300 DPI PNG) to reduce Vision API payload size/cost.")
    lines.append(f"- Vision model routing: Anthropic Claude 3.5 Sonnet primary, OpenAI GPT-4o automatic "
                  f"fallback on error — avoids double API cost on the happy path.")
    lines.append(f"- Single combined multi-image message per iteration (original + generated) instead of "
                  f"two separate calls — halves round-trips.")
    lines.append(f"- Targeted regex-based CSS patching (not full-file LLM rewrite) — auditable, minimal-diff, "
                  f"and structurally incapable of altering HTML/content.")
    lines.append(f"- Guardrails from `cv_rendering_rules.md` enforced pre-patch: forbidden properties/values "
                  f"(e.g. `text-align: justify`) and structural selectors are filtered out before any patch is applied.")
    lines.append("")

    lines.append("## Convergence History\n")
    lines.append("| Iteration | Score | Provider | Patches Applied | Patches Rejected |")
    lines.append("|---|---|---|---|---|")
    for h in history:
        score = h.get("score")
        score_str = str(score) if score is not None else "ERROR"
        lines.append(
            f"| {h['iteration']} | {score_str} | {h.get('provider','-')} | "
            f"{len(h.get('issues_applied', []))} | {len(h.get('issues_rejected', []))} |"
        )
    lines.append("")

    lines.append("## Detailed Iteration Log\n")
    for h in history:
        lines.append(f"### Iteration {h['iteration']}")
        if h.get("error"):
            lines.append(f"- **Error:** {h['error']}")
            continue
        lines.append(f"- **Provider:** {h.get('provider')}")
        lines.append(f"- **Similarity score:** {h.get('score')}")
        lines.append(f"- **Vision summary:** {h.get('vision_summary','')}")
        if h.get("issues_applied"):
            lines.append("- **Patches applied:**")
            for p in h["issues_applied"]:
                lines.append(f"  - {p}")
        if h.get("issues_rejected"):
            lines.append("- **Patches rejected (guardrail):**")
            for r in h["issues_rejected"]:
                lines.append(f"  - {r}")
        lines.append("")

    lines.append("## Final CSS State\n")
    lines.append("See `cv_style.css` (live) and `_css_history/` for a snapshot of the stylesheet "
                 "before each iteration's patch, for full before/after auditability.")
    lines.append("")

    QA_REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
