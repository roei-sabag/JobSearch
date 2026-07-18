"""
compare_agents_verbose.py
--------------------------
Debug/verification script (NOT part of the running app) that shows, for a
single JD, EACH individual LLM's raw independent answer (Claude, GPT-4o),
PLUS Gemini's final arbitrated/reconciled answer and its stated rationale
for every reconciliation decision - so you can see exactly what each model
proposed on its own, and how/why the final consensus differs from each of
them individually.

Usage:
    python compare_agents_verbose.py            # uses the most recent job in jobs.db
    python compare_agents_verbose.py --job-id 12 # uses a specific job by ID
"""
import argparse
import json
import sqlite3

import tailor_skills as ts


def get_jd_text(job_id: int | None) -> tuple[str, int]:
    con = sqlite3.connect("jobs.db")
    cur = con.cursor()
    if job_id is not None:
        cur.execute("SELECT id, raw_description FROM jobs WHERE id = ?", (job_id,))
    else:
        cur.execute("SELECT id, raw_description FROM jobs ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"No job found (job_id={job_id})")
    return row[1], row[0]


def print_header(title: str):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def print_candidate(label: str, response) -> None:
    print(f"\n--- {label} ---")
    print("soft_skills_line:")
    print(" ", response.soft_skills_line)
    print("seeking_line:")
    print(" ", response.seeking_line)
    print("categories:")
    for c in response.categories:
        print(f"  [{c.name}]: {', '.join(c.skills)}")
    if getattr(response, "rationale", None):
        print("rationale:")
        for r in response.rationale:
            print("  -", r)
    if getattr(response, "omitted", None):
        print("omitted:")
        for o in response.omitted:
            print("  -", o)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, default=None)
    args = parser.parse_args()

    jd_text, job_id = get_jd_text(args.job_id)
    pool = ts.load_json(ts.SKILLS_POOL_PATH)

    print_header(f"JOB #{job_id} - JD TEXT (first 300 chars)")
    print(jd_text[:300])

    print_header("STEP 1/3 - CLAUDE (Anthropic) - independent answer")
    claude_response = ts.tailor_with_anthropic(jd_text, pool)
    print_candidate("Claude", claude_response)

    print_header("STEP 2/3 - GPT-4o (OpenAI) - independent answer")
    openai_response = ts.tailor_with_openai(jd_text, pool)
    print_candidate("GPT-4o", openai_response)

    print_header("STEP 3/3 - GEMINI ARBITER - reconciled final answer")
    candidate_a = ts._candidate_to_dict(claude_response)
    candidate_b = ts._candidate_to_dict(openai_response)
    try:
        arbiter_response = ts._reconcile_with_gemini(jd_text, pool, candidate_a, candidate_b)
        print_candidate("Gemini (final consensus)", arbiter_response)
    except Exception as e:
        print(f"\n!! Gemini arbiter FAILED: {type(e).__name__}: {e}")
        print("(In production, tailor_with_multi_agent_consensus() would now fall back")
        print(" to a deterministic tiebreak between Claude and GPT-4o - see _tiebreak().)")
        arbiter_response = None

    print_header("SIDE-BY-SIDE DIFF SUMMARY")

    def skills_set(resp):
        s = set()
        for c in resp.categories:
            s.update(c.skills)
        return s

    claude_skills = skills_set(claude_response)
    openai_skills = skills_set(openai_response)

    print("Claude soft_skills_line: ", claude_response.soft_skills_line)
    print("GPT-4o soft_skills_line: ", openai_response.soft_skills_line)
    if arbiter_response:
        print("Final  soft_skills_line: ", arbiter_response.soft_skills_line)

    print()
    print("Claude seeking_line: ", claude_response.seeking_line)
    print("GPT-4o seeking_line: ", openai_response.seeking_line)
    if arbiter_response:
        print("Final  seeking_line: ", arbiter_response.seeking_line)

    print()
    print("Skills Claude proposed but GPT-4o did not:", claude_skills - openai_skills or "(none)")
    print("Skills GPT-4o proposed but Claude did not:", openai_skills - claude_skills or "(none)")
    print("Skills both agreed on:                    ", claude_skills & openai_skills or "(none)")

    if arbiter_response:
        final_skills = skills_set(arbiter_response)
        print()
        print("Final skills chosen by arbiter:            ", final_skills)
        print("  -> kept from Claude only:  ", final_skills & (claude_skills - openai_skills) or "(none)")
        print("  -> kept from GPT-4o only:  ", final_skills & (openai_skills - claude_skills) or "(none)")
        print("  -> dropped by arbiter (proposed by either, but excluded from final):",
              (claude_skills | openai_skills) - final_skills or "(none)")


if __name__ == "__main__":
    main()
