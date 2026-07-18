"""
compare_agents.py
------------------
One-off debug/verification script (NOT part of the running app) that proves
the multi-agent consensus pipeline actually changes the output vs. the old
single-agent (Anthropic-only) path, by running BOTH on the EXACT SAME JD
text (eliminating the "different JD text" confound found while debugging
Job #4 vs Job #12).

Usage:
    python compare_agents.py
"""
import json
import sqlite3

import tailor_skills as ts

# Pull the real JD text straight from the DB (Job #12's raw_description) so
# this is a genuine real-world JD, not a synthetic one.
con = sqlite3.connect("jobs.db")
cur = con.cursor()
cur.execute("SELECT raw_description FROM jobs WHERE id = 12")
jd_text = cur.fetchone()[0]

pool = ts.load_json(ts.SKILLS_POOL_PATH)

print("=" * 70)
print("JD TEXT (first 200 chars):", jd_text[:200])
print("=" * 70)

print("\n--- Running SINGLE-AGENT (Anthropic Claude only, the OLD path) ---")
single = ts.tailor_with_anthropic(jd_text, pool)
print("soft_skills_line:", single.soft_skills_line)
print("seeking_line:", single.seeking_line)
print("categories:", [(c.name, c.skills) for c in single.categories])

print("\n--- Running MULTI-AGENT CONSENSUS (Claude + GPT-4o + Gemini arbiter, the NEW path) ---")
multi, mode = ts.tailor_with_multi_agent_consensus(jd_text, pool)
print("mode:", mode)
print("soft_skills_line:", multi.soft_skills_line)
print("seeking_line:", multi.seeking_line)
print("categories:", [(c.name, c.skills) for c in multi.categories])

print("\n" + "=" * 70)
print("DIFF SUMMARY")
print("=" * 70)
print("soft_skills_line changed:", single.soft_skills_line != multi.soft_skills_line)
print("seeking_line changed:", single.seeking_line != multi.seeking_line)

single_skills = set()
for c in single.categories:
    single_skills.update(c.skills)
multi_skills = set()
for c in multi.categories:
    multi_skills.update(c.skills)

added = multi_skills - single_skills
removed = single_skills - multi_skills
print("Skills added by multi-agent vs single-agent:", added or "(none)")
print("Skills removed by multi-agent vs single-agent:", removed or "(none)")
