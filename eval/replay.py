"""E2: replay a real repo's history through the guard.

1. Build the graph as it was on CUT (only history before CUT; structure from HEAD).
2. Every real non-merge commit after CUT -> check() on its added lines. These are real, reviewed, merged changes,
   so a flag is either a deliberate reversal or a false alarm. We list every flag for a human to classify.
3. "Resurrections": for each guarded decision, take the real lines that were deleted when the library was dropped
   (the removal commit, and earlier commits that deleted its imports) and add them back. Should be flagged.
4. Read-context cost: context() for every file the replayed commits touched (a proxy for files an agent reads).

    python -m eval.replay <repo> [--cut 2024-01-01]
"""

import argparse
import json
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from precedent import config, db, scan, tools

OUT = Path(__file__).parent / "out"
CODE_GLOBS = ["*.py", "*.js", "*.ts", "*.tsx", "*.jsx"]
SKIP = re.compile(r"(\.min\.js|/dist/|\.po$|\.mo$|\.svg$|\.map$|package-lock\.json|yarn\.lock|\.lock$)")


def ms(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def commits_after(repo, cut):
    """Yield (sha, subject, {path: added_text}) for each non-merge commit after cut, streamed (no big buffers)."""
    p = subprocess.Popen(["git", "log", "--no-merges", "--reverse", f"--since={cut}", "-p", "-U0", "--no-color",
                          "--format=%x01%H%x00%s"], cwd=repo, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace")
    sha = subject = path = None
    files = {}
    for line in p.stdout:
        if line.startswith("\x01"):
            if sha:
                yield sha, subject, files
            sha, subject = line[1:].rstrip("\n").split("\x00", 1)
            files, path = {}, None
        elif line.startswith("+++ "):
            path = line[6:].rstrip("\n") if line.startswith("+++ b/") else None
        elif line.startswith("+") and path and not SKIP.search(path):
            files[path] = files.get(path, "") + line[1:]
    if sha:
        yield sha, subject, files


def deleted_lines(repo, sha, pattern=None):
    """{path: text} of lines deleted by a commit (optionally only lines matching pattern)."""
    diff = subprocess.run(["git", "show", "-U0", "--no-color", "--format=", sha], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout
    out, path = {}, None
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("-") and path and (pattern is None or re.search(pattern, line[1:])):
            out[path] = out.get(path, "") + line[1:] + "\n"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--cut", default="2024-01-01")
    ap.add_argument("--graph", default="replay")
    a = ap.parse_args()
    repo = str(Path(a.repo).resolve())
    config.GRAPH, config.REPO = a.graph, repo
    t0 = time.time()
    stats = scan.init(repo, name=a.graph, until=ms(a.cut), verbose=False)
    guarded = db.read("""MATCH (d:Decision {status: 'active', trust: 'historical'}) WHERE size(d.forbid) > 0
        OPTIONAL MATCH (d)-[:EVIDENCED_BY]->(c:Commit) RETURN d.id, d.title, d.forbid, c.sha""")
    print(f"graph as of {a.cut}: {stats['decisions']} decisions, {len(guarded)} guarded ({stats['seconds']} s)")

    # 2. real commits after the cut
    n_commits = n_files = 0
    flags, verdicts, lat = [], Counter(), []
    touched = Counter()
    for sha, subject, files in commits_after(repo, a.cut):
        n_commits += 1
        worst = "ok"
        for path, added in files.items():
            if added.count("\n") > 3000:
                continue
            n_files += 1
            touched[path] += 1
            t = time.perf_counter()
            r = tools.check(path, added, "")
            lat.append((time.perf_counter() - t) * 1000)
            if r["verdict"] in ("ask", "deny"):
                worst = max(worst, r["verdict"], key=tools.RANK.get)
                h = r["hits"][0]
                line = next((l for l in added.splitlines() if tools.violates(h["token"], path, l)), "")
                flags.append({"sha": sha[:10], "subject": subject, "path": path, "decision": h["id"],
                              "title": h["title"], "token": h["token"], "line": line.strip()[:120], "verdict": r["verdict"]})
        verdicts[worst] += 1

    # 3. resurrections of removed libraries, from the real deleted lines
    res = []
    for did, title, forbid, sha in guarded:
        names = [f.removeprefix("lib:") for f in forbid if f.startswith("lib:")]
        pat = "|".join(re.escape(n) for n in names)
        edits = deleted_lines(repo, sha, pat) if sha else {}
        older = subprocess.run(["git", "log", "--no-merges", f"--before={a.cut}", "--format=%H", "-i",
                                f"-G(import|from|require).*({pat})", "--", *CODE_GLOBS], cwd=repo, capture_output=True,
                               text=True).stdout.split()
        for s in older[:3]:
            for p_, t_ in deleted_lines(repo, s, rf"^\s*(import|from)\s+({pat})\b|(require\(|from|import)\s*\(?\s*[\"']({pat})[\"'/]").items():
                edits.setdefault(p_, t_)
        if not edits:
            continue
        caught = [p_ for p_, t_ in edits.items() if any(h["id"] == did for h in tools.check(p_, t_, "")["hits"])]
        comment_only = [p_ for p_, t_ in edits.items() if p_ not in caught and all(
            not l.strip() or l.strip().startswith(("#", "//")) for l in t_.splitlines())]
        res.append({"decision": did, "title": title, "edits": len(edits), "caught": len(caught),
                    "missed_comment_only": len(comment_only), "missed_real": len(edits) - len(caught) - len(comment_only),
                    "example": next(iter(edits.items()))[1].strip().splitlines()[0][:100]})

    # 4. what the Read hook would add for files people actually touched
    sizes = []
    for path in list(touched)[:1500]:
        text, _ = tools.context([path])
        sizes.append(len(text))
    nonempty = [s for s in sizes if s]
    lat.sort()

    # 5. ground truth from the future: was each decision mined at the cut still true at HEAD (library not back in any
    # manifest)? A flag on a decision that was later reversed is the guard asking about a real reversal, not noise.
    _, head_present, _ = scan.manifest_history(repo)
    now = set().union(*head_present.values()) if head_present else set()
    mined = db.read("""MATCH (d:Decision) OPTIONAL MATCH (d)-[:REJECTED]->(l:Library)
        RETURN d.id, d.title, size(d.forbid) > 0, collect(l.name)""")
    reversed_ids = {i for i, _, _, libs in mined if any(l in now for l in libs)}
    for f in flags:
        f["kind"] = "reversal (library came back later)" if f["decision"] in reversed_ids else "false alarm"
    held = [i for i, _, _, _ in mined if i not in reversed_ids]
    report = {
        "repo": Path(repo).name, "cut": a.cut, "graph_at_cut": stats, "guarded_decisions": len(guarded),
        "decisions_at_cut": len(mined), "decisions_still_true_at_head": len(held),
        "reversed_decisions": sorted(reversed_ids, key=lambda i: int(i[2:])),
        "replayed_commits": n_commits, "replayed_file_changes": n_files, "commit_verdicts": dict(verdicts),
        "flag_rate_commits": round((verdicts["ask"] + verdicts["deny"]) / max(n_commits, 1), 4),
        "flagged_commits_false_alarm": len({f["sha"] for f in flags if f["kind"] == "false alarm"}),
        "flagged_commits_reversal": len({f["sha"] for f in flags if f["kind"] != "false alarm"}),
        "decisions": [{"id": i, "title": t, "guarded": g, "still_true_at_head": i not in reversed_ids}
                      for i, t, g, _ in mined],
        "flags": flags,
        "resurrections": res,
        "resurrection_edits": sum(r["edits"] for r in res), "resurrection_caught": sum(r["caught"] for r in res),
        "resurrection_missed_comment_only": sum(r["missed_comment_only"] for r in res),
        "resurrection_missed_real": sum(r["missed_real"] for r in res),
        "decisions_with_resurrection_tested": len(res),
        "decisions_caught": sum(1 for r in res if r["caught"]),
        "check_ms_p50": round(lat[len(lat) // 2], 2) if lat else None,
        "check_ms_p95": round(lat[int(len(lat) * 0.95)], 2) if lat else None,
        "read_context_files": len(sizes), "read_context_nonempty": len(nonempty),
        "read_context_avg_tokens_when_shown": round(sum(nonempty) / max(len(nonempty), 1) / 4),
        "seconds": round(time.time() - t0, 1),
    }
    OUT.mkdir(exist_ok=True)
    (OUT / f"replay_{Path(repo).name}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k not in ("flags", "resurrections", "graph_at_cut", "decisions")},
                     indent=1))
    for f in flags[:60]:
        print(f"FLAG {f['verdict']} [{f['kind']}] {f['sha']} {f['path']} {f['decision']} {f['token']} | {f['line']} | "
              f"{f['subject'][:70]}")
    for r in res:
        print(f"RES {r['decision']} {r['caught']}/{r['edits']} (missed real {r['missed_real']}) {r['title'][:60]} | {r['example']}")


if __name__ == "__main__":
    main()
