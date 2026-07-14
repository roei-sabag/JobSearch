# CV Rendering & Tailoring Rules ("Engineering Constitution")

This document is the binding rule-set for all automated CV template rendering,
resume-tailoring, and visual-QA correction performed by this pipeline
(`tailor_skills.py`, `visual_qa_loop.py`, and any future modules). Any
auto-correction step (including LLM-driven CSS patches) MUST comply with
these rules. Rules are grouped by concern.

---

## 1. Typography & Alignment Constraints

- **No arbitrary text justification.** Body/paragraph text (summary,
  bullet points, entry subtitles) must use `text-align: left` (natural
  ragged-right flow) unless a measurement of the original
  `Roei_Sabag_CV.pdf` explicitly proves the original document itself uses
  full justification. Justification is the default failure mode we've
  already diagnosed and fixed once (Phase 1.5 root-cause: `.summary`
  `text-align: justify` caused unnatural word-gap stretching) — it must
  not be silently reintroduced by any auto-correction loop.
- **No unconstrained flex stretching.** Flex containers used for
  title/date pairs (`.section-title`, `.entry-header`) may use
  `justify-content: space-between` for legitimate two-point alignment
  (label left, date right), but must always set `flex-wrap: nowrap`,
  `align-items: baseline`, and an explicit `gap` to prevent collision or
  unnatural stretching on long text.
- **Fixed structural layout.** Section order, section titles, header
  layout (name/contact/title block), and overall document skeleton are
  FROZEN. Auto-correction may only adjust *cosmetic* CSS properties
  (font-size, margin, padding, line-height, color, gap, text-align on
  specific elements) — never the HTML structure, section ordering, or
  the Jinja2 placeholder mechanism itself.
- **Font sizing in `pt`, spacing in `pt`/`cm`.** All sizing must remain
  in print-accurate absolute units (`pt`, `cm`, `mm`) — never `%` or
  viewport-relative units — so the rendered PDF is deterministic
  regardless of rendering engine (Chromium print-to-PDF, or WeasyPrint
  if reintroduced later).

## 2. Content Guardrails (100% Truthfulness)

- The system is **strictly forbidden** from fabricating, inventing, or
  inferring any of the following beyond what is explicitly present in
  `all_my_skills.txt` and `Roei_Sabag_CV.pdf`:
  - Skills, tools, or technologies (enforced today via the
    `validate_authenticity()` guardrail in `tailor_skills.py`, which
    strips any skill not present in `skills_pool.json`).
  - Projects, employers, job titles, or role descriptions.
  - Dates, GPA, degree status, or graduation year.
  - Any claim of certification, award, or credential not present in the
    source documents.
- Any LLM call involved in tailoring or QA must be given the ground-truth
  source text as context and instructed explicitly that fabrication is a
  critical failure, not a stylistic preference.
- Visual QA auto-correction (Phase 1.5) only ever modifies **CSS
  properties**, never textual content — so this guardrail is structurally
  enforced by scope, in addition to the explicit LLM instruction.

## 3. Overflow Protections

- Dynamic content blocks (currently: the Skills section, rendered via a
  Jinja2 loop) must never be allowed to visually overflow their container
  or push the document to a second page.
- Enforcement layers (defense in depth):
  1. **Upstream constraint (preferred):** `tailor_skills.py` already caps
     categories (`MAX_CATEGORIES`), items per category
     (`MAX_SKILLS_PER_CATEGORY`), and line length
     (`MAX_CATEGORY_LINE_CHARS`) *before* any text reaches the template.
  2. **CSS-level safety net:** `.skills-block` and similar dynamic
     containers must declare `overflow: hidden` and (where feasible)
     `max-height` bounded to the space available in the fixed one-page
     layout, so that any unexpected overflow is clipped rather than
     breaking the grid or pushing content onto a second page.
  3. **Last-resort shrink:** if overflow risk is detected, prefer a small
     `font-size` reduction on the specific dynamic block (e.g. via a CSS
     class toggle) over allowing layout breakage — this must be scoped
     only to the dynamic block, never applied document-wide.

## 3a. Relevant Coursework Block (Education Section)

- The "Relevant Coursework" line under Education is fully **optional/conditional**:
  if `select_relevant_courses()` (in `tailor_skills.py`) returns an empty list
  for a given JD, the entire `{% if relevant_courses %}` block in
  `cv_template.html` MUST be omitted from the rendered CV — never render an
  empty "Relevant Coursework:" label with no courses after it.
- A course is only ever eligible if BOTH conditions hold:
  1. It has a genuine numeric grade in the 80–100 range (ground-truth pool in
     `courses_pool.json` already pre-filters this; `Pass`/`Exempt`/`400`
     binary-pass codes/`F` grades are never eligible).
  2. It scores as relevant to the specific JD via curated `tags` overlap
     (never via raw course-name word overlap — course titles often contain
     generic terms like "Electrical"/"Engineering" that would falsely match
     almost any EE-related JD).
- This selection is always deterministic/rule-based (never LLM-generated),
  since it deals with real academic grades — a domain where fabrication or
  misattribution risk must be zero.

## 3b. Header Title Line & Role-Qualifier Human-in-the-Loop Configuration

- The header's title line (previously the hardcoded string "Electrical &amp;
  Computer Engineering Student (2 semesters remaining)") is now built
  dynamically via `build_title_line()` in `tailor_skills.py`, and is
  human-in-the-loop configurable per tailoring run via the "Header Title
  Line" panel in the UI (`static/index.html`) and the
  `/api/jobs/{job_id}/finalize-courses` endpoint's `semesters_remaining` /
  `include_student_in_title` fields:
  - `semesters_remaining` may be `0`, `1`, `2`, or `3` (clamped to this
    range). When `0`, the parenthetical "(N semesters remaining)" is
    **omitted entirely** — never rendered as "(0 semesters remaining)".
  - `include_student_in_title` toggles whether the word "Student" appears
    in the title line at all (e.g. "Electrical &amp; Computer Engineering"
    vs. "Electrical &amp; Computer Engineering Student").
  - The degree/major name itself (`DEFAULT_TITLE_ROLE`) is NEVER
    user-editable — this stays a frozen, ground-truth string, since
    fabricating a different degree/major would be an authenticity
    violation per Section 2 above.
- The `seeking_line`'s role qualifier ("Seeking a {role} Student
  position..." vs. "...Junior position...") is similarly configurable via
  the `role_qualifier` field (`"Student"` or `"Junior"`, validated against
  `ALLOWED_ROLE_QUALIFIERS` server-side — any other value silently falls
  back to the default `"Student"`, never trusting arbitrary client input).
  Both qualifiers preserve the same non-fabrication intent (never implying
  senior professional standing) — this is a widening of phrasing choice,
  not a weakening of the authenticity guardrail in Section 2.

## 3c. Skills Section Human-in-the-Loop Selection

- In addition to the algorithm's (LLM/fallback) automatic skill
  prioritization, the user may now manually pick exactly which skills
  from their own ground-truth `skills_pool.json` appear in the CV's
  Skills section, via the "Skills" picker panel in the UI and the
  `selected_skills` field on `/api/jobs/{job_id}/finalize-courses`.
- Every server-side authenticity guardrail from Section 2 still applies
  unconditionally: `validate_skill_selection_authenticity()` re-checks
  every user-selected skill against the ground-truth pool (name AND
  category must match exactly) before it is rendered — a human clicking a
  checkbox can never bypass the "never fabricate a hard skill" rule,
  since the picker itself only ever offers skills that already exist in
  `skills_pool.json`.
- If the user's selection would leave every category empty, the previous
  run's full category/skill selection is left unchanged rather than
  rendering an empty Skills section — this selection is always additive
  filtering (narrowing which of the algorithm's already-authentic skills
  are shown), never an avenue to add anything new.

## 4. Auto-Correction Loop Governance (Visual QA)



- The Vision-LLM-driven auto-correction loop (`visual_qa_loop.py`) may
  only apply **targeted, auditable CSS patches** corresponding to
  specific `(selector, property, suggested_value)` triples returned in
  structured JSON feedback. It must never regenerate or replace the
  entire stylesheet in one shot.
- Each patch application must be logged (before/after value, iteration
  number, similarity score at that step) for full auditability.
- The loop must never touch: HTML structure, the Jinja2 skills loop, or
  any rule in Section 1–3 above (e.g. it cannot "fix" a low similarity
  score by reintroducing `text-align: justify`, even if a naive vision
  model suggests it — such a suggestion must be rejected/filtered).
- Stop conditions: similarity score ≥ 95%, OR 5 iterations reached, OR
  score improvement between consecutive iterations < 2 points
  (plateau) — whichever occurs first.
