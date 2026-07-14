# Visual QA Convergence Report

**Final output uses the BEST-scoring CSS state found: 88/100** (not necessarily the last iteration's state, to protect against Vision-LLM scoring noise).

## Optimization Choices

- PDF rasterization: PyMuPDF (`fitz`) at 150 DPI, resized to max 1000px long edge, re-encoded as JPEG q=80 (instead of print-native 300 DPI PNG) to reduce Vision API payload size/cost.
- Vision model routing: Anthropic Claude 3.5 Sonnet primary, OpenAI GPT-4o automatic fallback on error — avoids double API cost on the happy path.
- Single combined multi-image message per iteration (original + generated) instead of two separate calls — halves round-trips.
- Targeted regex-based CSS patching (not full-file LLM rewrite) — auditable, minimal-diff, and structurally incapable of altering HTML/content.
- Guardrails from `cv_rendering_rules.md` enforced pre-patch: forbidden properties/values (e.g. `text-align: justify`) and structural selectors are filtered out before any patch is applied.

## Convergence History

| Iteration | Score | Provider | Patches Applied | Patches Rejected |
|---|---|---|---|---|
| 1 | 88 | anthropic-claude-3.5-sonnet | 3 | 1 |
| 2 | 88 | anthropic-claude-3.5-sonnet | 3 | 2 |
| 3 | 82 | anthropic-claude-3.5-sonnet | 3 | 2 |

## Detailed Iteration Log

### Iteration 1
- **Provider:** anthropic-claude-3.5-sonnet
- **Similarity score:** 88
- **Vision summary:** The generated CV layout achieves high fidelity to the original with consistent structure, fonts, and overall positioning. Minor spacing discrepancies exist in text justification, divider margins, and bullet point line-height that slightly compress the vertical rhythm compared to the reference.
- **Patches applied:**
  - Patched existing 'margin-bottom' in 'hr.divider' -> 16px
  - Added new 'line-height: 1.5;' to existing 'ul.bullets li' block
  - Patched existing 'margin-bottom' in 'ul.bullets li' -> 8px
- **Patches rejected (guardrail):**
  - Rejected 'text-align: justify' on '.summary' - forbidden value per cv_rendering_rules.md.

### Iteration 2
- **Provider:** anthropic-claude-3.5-sonnet
- **Similarity score:** 88
- **Vision summary:** The generated layout achieves strong overall fidelity with the original, matching most spacing, typography, and structural elements. Primary discrepancies involve text justification in the summary, bullet indentation hierarchy, and minor spacing inconsistencies in the skills section formatting.
- **Patches applied:**
  - Added new 'margin-left: 20px;' to existing '.entry' block
  - Patched existing 'margin-bottom' in 'ul.bullets li' -> 2px
  - Patched existing 'margin-bottom' in 'hr.divider' -> 12px
- **Patches rejected (guardrail):**
  - Rejected 'text-align: justify' on '.summary' - forbidden value per cv_rendering_rules.md.
  - Rejected 'display' on '.skills-category' - property not in allowed cosmetic list.

### Iteration 3
- **Provider:** anthropic-claude-3.5-sonnet
- **Similarity score:** 82
- **Vision summary:** The generated layout achieves strong overall fidelity with correct section ordering, appropriate typography, and proper margins. Main discrepancies are in text justification of the summary, section title font weight, and Skills section formatting structure.
- **Patches applied:**
  - Patched existing 'margin-bottom' in 'hr.divider' -> 16px
  - Patched existing 'font-weight' in '.section-title-text' -> 700
  - Added new 'line-height: 1.5;' to existing '.entry' block
- **Patches rejected (guardrail):**
  - Rejected 'text-align: justify' on '.summary' - forbidden value per cv_rendering_rules.md.
  - Rejected 'display' on '.skills-block' - property not in allowed cosmetic list.

## Final CSS State

See `cv_style.css` (live) and `_css_history/` for a snapshot of the stylesheet before each iteration's patch, for full before/after auditability.
