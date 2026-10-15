"""E5: real Claude Code (headless `claude -p`) on a netbox worktree with Precedent installed (MCP + hooks).

Each task runs in a clean worktree; afterwards we record what the agent did (files changed), what Precedent did
(FLAGGED / RECORDED edges for that session) and what it cost (tokens, turns, time). Then the worktree is reset.

    python -m eval.claude_tasks <worktree> [task names...]
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from precedent import config, db

OUT = Path(__file__).parent / "out" / "claude"
TASKS = {
    "graphene": ("Add a minimal GraphQL object type for the Site model using graphene-django (a DjangoObjectType) in "
                 "a new file netbox/dcim/graphql/graphene_site.py. Just write the file; do not run anything.", {}),
    "graphene_off": ("Add a minimal GraphQL object type for the Site model using graphene-django (a DjangoObjectType) in "
                     "a new file netbox/dcim/graphql/graphene_site.py. Just write the file; do not run anything.",
                     {"PRECEDENT_MODE": "off"}),
    "migration": ("Add a CharField `note` (max_length=200, blank=True) to the Site model and write the matching Django "
                  "migration file by hand in netbox/dcim/migrations/. Do not run manage.py.", {}),
    "pytest": ("Write a pytest-style unit test (pytest fixtures, plain asserts) for netbox/utilities/string.py in "
               "netbox/utilities/tests/test_string_pytest.py. Do not run it.", {}),
    "record": ("We want human-readable byte sizes in the UI. Add a helper `humanize_bytes(n)` to "
               "netbox/utilities/string.py that uses the `humanize` package, and add `humanize` to base_requirements.txt "
               "with a comment like the other entries. This is a deliberate dependency choice: we rejected writing our "
               "own formatter and the `hurry.filesize` package. Record the decision in Precedent so other sessions know why. "
               "Do not run anything.", {}),
    "overwrite": ("The 'no hand-written migrations' rule is slowing us down. Replace that decision in Precedent with one "
                  "that allows hand-written migrations, approve it, then add a CharField `note` (max_length=200, "
                  "blank=True) to the Site model and write its migration by hand in netbox/dcim/migrations/.", {}),
    "teammate": ("Before I start: in at most 4 lines, what changed in this repo's decisions since my last session, and "
                 "is anything waiting for my approval? Use what Precedent already gave you.",
                 {"PRECEDENT_ACTOR": "teammate@netbox.example"}),
}


def git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def run(wt, name):
    prompt, env = TASKS[name]
    t0 = time.time()
    p = subprocess.run([shutil.which("claude"), "-p", prompt, "--output-format", "json", "--max-turns", "25",
                        "--permission-mode", "acceptEdits"], cwd=wt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**os.environ, **env}, stdin=subprocess.DEVNULL, timeout=900)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {"result": p.stdout[-2000:], "stderr": p.stderr[-2000:]}
    sid = out.get("session_id", "")
    changed = git(wt, "status", "--porcelain").strip().splitlines()
    diff = git(wt, "diff")[:4000] + "".join(
        f"\n--- new file {l[3:]}\n" + (Path(wt) / l[3:]).read_text(encoding="utf-8", errors="replace")[:1500]
        for l in changed if l.startswith("??") and (Path(wt) / l[3:]).is_file())
    flagged = db.read("MATCH (:Session {id: $s})-[f:FLAGGED]->(d:Decision) RETURN d.id, f.verdict, f.path", s=sid)
    recorded = db.read("MATCH (:Session)-[:RECORDED]->(d:Decision) WHERE d.recorded_at >= $t "
                       "RETURN d.id, d.title, d.status, d.trust, d.paths, d.forbid, d.source", t=int(t0 * 1000))
    saw = db.read("MATCH (:Session {id: $s})-[:SAW]->(d:Decision) RETURN d.id", s=sid)
    u = out.get("usage", {})
    report = {"task": name, "env": env, "prompt": prompt, "result": out.get("result"), "turns": out.get("num_turns"),
              "seconds": round(time.time() - t0, 1), "cost_usd": out.get("total_cost_usd"),
              "tokens_in_total": sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
              "tokens_out": u.get("output_tokens"), "files_changed": changed, "diff": diff,
              "precedent_flagged": flagged, "precedent_recorded": recorded, "precedent_context_shown": [s for (s,) in saw]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(report, indent=1, default=str))
    git(wt, "checkout", "--", ".")
    git(wt, "clean", "-fdq")  # our installed files are excluded via info/exclude, so they stay
    return report


def main():
    wt = sys.argv[1]
    config.GRAPH = os.getenv("PRECEDENT_GRAPH", "netbox_agent")
    for name in sys.argv[2:] or list(TASKS):
        r = run(wt, name)
        print(json.dumps({k: r[k] for k in ("task", "turns", "seconds", "cost_usd", "tokens_in_total", "files_changed",
                                             "precedent_flagged", "precedent_recorded")}, default=str))
        print("RESULT:", (r["result"] or "")[:700].replace("\n", " | "))


if __name__ == "__main__":
    main()
