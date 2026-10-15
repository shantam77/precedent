"""Command line for humans (and git hooks).

  python -m precedent init <repo>               cold start: scan code + git into the graph (0 LLM tokens)
  python -m precedent install <repo>            wire Claude Code (MCP + hooks + permissions) and a git pre-commit check
  python -m precedent review                    proposals waiting for a human
  python -m precedent approve|reject <id>       decide on a proposal
  python -m precedent revert <change> <why>     undo any change
  python -m precedent log [--hours N]           the graph's change log
  python -m precedent why|impact|who|context    ask questions
  python -m precedent check-staged              git pre-commit: deny-level violations block the commit
  python -m precedent team <name> --members a@x --owns comp    owners for approvals of locked decisions
  python -m precedent lock <id>                 only the owner team can change it afterwards
  python -m precedent export-md                 DECISIONS.md, compiled from the graph
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config

ROOT = Path(__file__).resolve().parent.parent


def install(repo, graph):
    repo = Path(repo).resolve()
    py = (ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).as_posix()
    env = {"PRECEDENT_GRAPH": graph, "PRECEDENT_REPO": repo.as_posix(), "PRECEDENT_HARNESS": "claude-code",
           "PYTHONPATH": ROOT.as_posix()}
    hook = {"type": "command", "command": f'"{py}" -m precedent.hook', "timeout": 15}
    settings_file = repo / ".claude" / "settings.local.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    settings.setdefault("env", {}).update(env)
    settings["enableAllProjectMcpServers"] = True
    reads = ["context", "check", "why", "impact", "who_knows", "history", "search", "query", "pending"]
    writes = ["record_decision", "supersede", "amend", "retire", "revert"]  # policy-gated on the server
    perms = settings.setdefault("permissions", {})
    perms["allow"] = sorted(set(perms.get("allow", [])) | {f"mcp__precedent__{t}" for t in reads + writes})
    perms["ask"] = sorted(set(perms.get("ask", [])) | {"mcp__precedent__approve"})
    settings["hooks"] = {
        "SessionStart": [{"hooks": [hook]}],
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [hook]},
                       {"matcher": "mcp__precedent__approve", "hooks": [hook]}],
        "PostToolUse": [{"matcher": "Read|Grep|Glob|Edit|Write|MultiEdit", "hooks": [hook]}],
    }
    settings_file.parent.mkdir(exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2))
    commands = repo / ".claude" / "commands"
    commands.mkdir(exist_ok=True)
    (commands / "precedent-onboard.md").write_text((ROOT / "precedent" / "assets" / "precedent-onboard.md").read_text())
    mcp_file = repo / ".mcp.json"
    mcp = json.loads(mcp_file.read_text()) if mcp_file.exists() else {}
    mcp.setdefault("mcpServers", {})["precedent"] = {"command": py, "args": ["-m", "precedent.mcp_server"],
                                                    "env": {**env, "PRECEDENT_HUMAN_GATE": "1"}}
    mcp_file.write_text(json.dumps(mcp, indent=2))
    hooks_dir = Path(subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=repo, capture_output=True,
                                    text=True).stdout.strip())
    hooks_dir = hooks_dir if hooks_dir.is_absolute() else repo / hooks_dir
    pre = hooks_dir / "pre-commit"
    pre.write_text(f'#!/bin/sh\nPRECEDENT_GRAPH="{graph}" PRECEDENT_REPO="{repo.as_posix()}" PYTHONPATH="{ROOT.as_posix()}" '
                   f'"{py}" -m precedent check-staged\n')
    pre.chmod(0o755)
    exclude = Path(subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=repo, capture_output=True,
                                  text=True).stdout.strip())
    exclude = exclude if exclude.is_absolute() else repo / exclude
    if exclude.parent.exists():
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        exclude.write_text("\n".join(lines + [l for l in (".mcp.json", ".claude/settings.local.json", ".claude/commands/precedent-onboard.md")
                                                    if l not in lines]) + "\n")
    print(f"Installed into {repo}: .mcp.json, .claude/settings.local.json (hooks + permissions), "
          "/precedent-onboard command, git pre-commit.")


def check_staged(strict=False):
    from . import tools
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"], cwd=config.REPO, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout
    worst, msgs = "ok", []
    for chunk in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
        m = re.search(r"^\+\+\+ b/(.+)$", chunk, re.M)
        if not m:
            continue
        added = "\n".join(l[1:] for l in chunk.splitlines() if l.startswith("+") and not l.startswith("+++"))
        r = tools.check(m.group(1), added, "")
        if r["verdict"] in ("ask", "deny"):
            msgs.append(r["text"])
            worst = max(worst, r["verdict"], key=tools.RANK.get)
    if msgs:
        print("\n\n".join(msgs), file=sys.stderr)
    if worst == "deny" or (strict and worst == "ask"):
        print("Commit blocked by Precedent.", file=sys.stderr)
        return 1
    return 0


def export_md():
    from . import db, tools
    rows = db.read("""MATCH (d:Decision {status: 'active'})-[:ABOUT]->(x)
        RETURN x.name, d.id, d.title, d.trust, d.rationale, d.source, d.valid_from ORDER BY x.name, d.valid_from""")
    out, cur = ["# Decisions (compiled from the Precedent graph; edit the graph, not this file)\n"], None
    for x, i, t, trust, why, src, since in rows:
        if x != cur:
            out.append(f"\n## {x}\n")
            cur = x
        out.append(f"- **{i}** {t} _({trust}, since {tools.day(since)}, {src})_  \n  {why[:300]}")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="precedent")
    ap.add_argument("--graph")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("repo"); p.add_argument("--until")
    p = sub.add_parser("install"); p.add_argument("repo")
    sub.add_parser("review"); sub.add_parser("stats"); sub.add_parser("export-md")
    p = sub.add_parser("approve"); p.add_argument("id")
    p = sub.add_parser("reject"); p.add_argument("id"); p.add_argument("reason")
    p = sub.add_parser("revert"); p.add_argument("change"); p.add_argument("reason")
    p = sub.add_parser("lock"); p.add_argument("id")
    p = sub.add_parser("log"); p.add_argument("--hours", type=int, default=24 * 7)
    p = sub.add_parser("why"); p.add_argument("target"); p.add_argument("--as-of")
    p = sub.add_parser("impact"); p.add_argument("target")
    p = sub.add_parser("who"); p.add_argument("target")
    p = sub.add_parser("context"); p.add_argument("paths", nargs="+")
    p = sub.add_parser("query"); p.add_argument("cypher")
    p = sub.add_parser("check-staged"); p.add_argument("--strict", action="store_true")
    p = sub.add_parser("team"); p.add_argument("name"); p.add_argument("--members", nargs="*", default=[])
    p.add_argument("--owns", nargs="*", default=[])
    a = ap.parse_args(argv)
    if a.cmd in ("init", "install") and not a.graph:
        a.graph = os.getenv("PRECEDENT_GRAPH") or Path(a.repo).resolve().name
    if a.graph:
        config.GRAPH = a.graph
    if a.cmd == "init":
        from . import scan
        from datetime import datetime, timezone
        until = int(datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc).timestamp() * 1000) if a.until else None
        config.REPO = str(Path(a.repo).resolve())
        scan.init(a.repo, name=a.graph, until=until)
        return 0
    if a.cmd == "install":
        install(a.repo, a.graph)
        return 0
    if a.cmd == "check-staged":
        return check_staged(a.strict)
    if a.cmd == "export-md":
        print(export_md())
        return 0
    from . import store, tools
    me = store.me("human")
    r = {
        "review": lambda: "\n".join(f"{p['id']} {p['title']} (by {p['actor']}"
                                    + (f", replaces {p['replaces']} '{p['replaces_title']}')" if p["replaces"] else ")")
                                    for p in tools.pending()) or "No pending proposals.",
        "stats": lambda: json.dumps(tools.stats()),
        "approve": lambda: store.approve(me, a.id)["text"],
        "reject": lambda: store.reject(me, a.id, a.reason)["text"],
        "revert": lambda: store.revert(me, a.change, a.reason)["text"],
        "lock": lambda: store.lock(me, a.id)["text"],
        "log": lambda: tools.history(tools.now_ms() - a.hours * 3600 * 1000, limit=50),
        "why": lambda: tools.why(a.target, a.as_of),
        "impact": lambda: tools.impact(a.target),
        "who": lambda: tools.who_knows(a.target),
        "context": lambda: tools.context(a.paths)[0] or "Nothing.",
        "query": lambda: tools.query(a.cypher),
        "team": lambda: (store.team(a.name, a.members, a.owns), f"Team {a.name} updated.")[1],
    }[a.cmd]()
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
