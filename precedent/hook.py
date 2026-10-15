"""Claude Code hooks, one script for every event (registered by `precedent install`).

  SessionStart                         -> a short brief + what changed since this person's last session
  PostToolUse  Read                    -> decisions/notes for that file, each one once per session
  PreToolUse   Edit|Write|MultiEdit    -> check(): deny / ask / allow, with the reason and source
  PostToolUse  Edit|Write|MultiEdit    -> Session-[:EDITED]->File; nudge when a new dependency appears
  PreToolUse   mcp__precedent__approve -> always ask the human (this is how a human approves from the harness)

Fail-open: any error lets the tool run and shows a warning (PRECEDENT_FAIL_CLOSED=1 denies edits instead).
Output format: https://code.claude.com/docs/en/hooks (hookSpecificOutput, permissionDecision, additionalContext).
"""

import json
import os
import re
import sys
import time

from . import config, db, scan, store, tools


def out(obj=None):
    if obj:
        print(json.dumps(obj))
    sys.exit(0)


def decide(event, decision, reason, context=None):
    o = {"hookEventName": event, "permissionDecision": decision, "permissionDecisionReason": reason}
    if context:
        o["additionalContext"] = context
    out({"hookSpecificOutput": o})


def add_context(event, text):
    out({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}} if text else None)


def edit_texts(tool, ti):
    """(new_text, old_text) the edit adds/removes. old_text None = compare with the file on disk."""
    if tool == "Write":
        return ti.get("content", ""), None
    if tool == "MultiEdit":
        edits = ti.get("edits", [])
        return "\n".join(e.get("new_string", "") for e in edits), "\n".join(e.get("old_string", "") for e in edits)
    return ti.get("new_string", ""), ti.get("old_string", "")


def session_start(ev, actor):
    store.ensure_session(actor)
    s = tools.stats()
    active = db.read("MATCH (d:Decision {status: 'active'}) RETURN count(d), sum(CASE WHEN size(d.forbid) > 0 THEN 1 ELSE 0 END)")[0]
    last = db.read("MATCH (p:Person) WHERE $e IN p.emails RETURN p.seen_at LIMIT 1", e=actor.email)
    since = (last[0][0] if last and last[0][0] else 0) or int(time.time() * 1000) - 7 * 86400 * 1000
    db.write("MATCH (p:Person) WHERE $e IN p.emails SET p.seen_at = timestamp()", e=actor.email)
    changes = db.read("""MATCH (s:Session)-[:MADE]->(ch:Change) WHERE ch.at > $since AND ch.actor <> $e
        OPTIONAL MATCH (ch)-[:ON]->(d:Decision)
        RETURN ch.id, ch.op, ch.actor, s.harness, collect(d.id + ' ' + d.title), ch.at AS at ORDER BY at DESC LIMIT 5""",
                      since=since, e=actor.email)
    pending = tools.pending()
    lines = [f"Precedent: this repo's decision graph (FalkorDB) has {active[0]} active decisions "
             f"({int(active[1] or 0)} guarded), {s['Component']} components, {s['Person']} people.",
             "Edits that break a decision are checked automatically (deny or ask); when you Read a file, the decisions "
             "about it are added to your context. Tools: context, why, impact, who_knows, record_decision, supersede.",
             "When you make a real choice (a library, a pattern, a rule), record it with record_decision(paths, rationale, evidence)."]
    if changes:
        lines.append("Changed by others since your last session:")
        lines += [f"- {cid} {op} by {who} ({h}): {'; '.join(ds)[:160]}" for cid, op, who, h, ds, _ in changes]
    if pending:
        lines.append(f"{len(pending)} proposal(s) wait for a human: " + ", ".join(f"{p['id']} {p['title'][:60]}" for p in pending[:3]))
    add_context("SessionStart", "\n".join(lines))


def after_read(ev, actor):
    path = tools.rel(ev.get("tool_input", {}).get("file_path", ""))
    seen = [i for (i,) in db.read("MATCH (:Session {id: $s})-[:SAW]->(d:Decision) RETURN d.id", s=actor.session)]
    text, shown = tools.context([path], skip_ids=seen)
    if shown:
        store.ensure_session(actor)
        db.write("MATCH (s:Session {id: $s}) UNWIND $ids AS i MATCH (d:Decision {id: i}) MERGE (s)-[:SAW]->(d)",
                 s=actor.session, ids=shown)
    add_context("PostToolUse", text)


def before_edit(ev, actor):
    ti = ev.get("tool_input", {})
    path = ti.get("file_path", "")
    new, old = edit_texts(ev.get("tool_name"), ti)
    r = tools.check(path, new, old)
    if r["verdict"] in ("ok", "note"):
        out()
    store.ensure_session(actor)
    db.write("""MATCH (s:Session {id: $s}) UNWIND $ids AS i MATCH (d:Decision {id: i})
        CREATE (s)-[:FLAGGED {verdict: $v, path: $p, at: timestamp()}]->(d)""",
             s=actor.session, ids=[h["id"] for h in r["hits"]], v=r["verdict"], p=tools.rel(path))
    decide("PreToolUse", r["verdict"], r["text"], context=r["text"] if r["verdict"] == "ask" else None)


def after_edit(ev, actor):
    path = tools.rel(ev.get("tool_input", {}).get("file_path", ""))
    store.ensure_session(actor)
    comp = tools.component_of(path)
    db.write("""MERGE (f:File {path: $p}) WITH f MATCH (s:Session {id: $s}) MERGE (s)-[:EDITED]->(f)
        WITH f MATCH (c:Component {name: $c}) WHERE NOT (f)-[:IN]->(:Component) CREATE (f)-[:IN]->(c)""",
             p=path, s=actor.session, c=comp)
    if not tools.MANIFEST.search(path):
        out()
    new, old = edit_texts(ev.get("tool_name"), ev.get("tool_input", {}))
    added = tools.added_text(new, old if old is not None else "")
    names = [scan.pep503(m.group(1)) for m in re.finditer(r"^\s*[\"']?([A-Za-z][\w.-]+)", added, re.M)
             if not m.group(1).startswith(("-", "#"))]
    known = {n for (n,) in db.read("MATCH (l:Library {present: true}) WHERE l.name IN $n RETURN l.name", n=names)}
    new_libs = [n for n in names if n not in known][:3]
    if new_libs:
        add_context("PostToolUse", f"Precedent: you added a new dependency ({', '.join(new_libs)}) to {path}. "
                    "If this is a deliberate choice, record it: record_decision(title, rationale, paths, chose=[...], "
                    "rejected=[alternatives you ruled out]) so other sessions and people know why.")
    out()


def before_approve(ev, actor):
    pid = ev.get("tool_input", {}).get("proposal_id", "")
    p = store.get(pid) or {}
    target = db.read("MATCH (:Decision {id: $p})-[r:PROPOSES]->(o:Decision) RETURN o.id, o.title, o.trust, r.op", p=pid)
    what = f"{target[0][3]} {target[0][0]} [{target[0][2]}] '{target[0][1]}'" if target else "activate it"
    decide("PreToolUse", "ask", f"Precedent: approve {pid} '{p.get('title', '?')}' by {p.get('actor', '?')}? "
           f"This will {what}. Only say yes if you (the human) agree.")


def main():
    ev = json.load(sys.stdin)
    if config.MODE == "off":
        out()
    if ev.get("cwd"):
        config.REPO = ev["cwd"]
    actor = store.Actor(config.actor_email(), "agent", config.HARNESS if config.HARNESS != "cli" else "claude-code",
                        ev.get("session_id", "unknown"))
    name, tool = ev.get("hook_event_name"), ev.get("tool_name", "")
    try:
        if name == "SessionStart":
            session_start(ev, actor)
        elif name == "PostToolUse" and tool == "Read":
            after_read(ev, actor)
        elif name == "PreToolUse" and tool in ("Edit", "Write", "MultiEdit"):
            before_edit(ev, actor)
        elif name == "PostToolUse" and tool in ("Edit", "Write", "MultiEdit"):
            after_edit(ev, actor)
        elif name == "PreToolUse" and tool.endswith("__approve"):
            before_approve(ev, actor)
        out()
    except SystemExit:
        raise
    except Exception as e:  # FalkorDB down, bad input...: never brick the editor unless asked to
        if config.FAIL_CLOSED and name == "PreToolUse":
            decide("PreToolUse", "deny", f"Precedent is unavailable ({e}); PRECEDENT_FAIL_CLOSED=1 blocks edits.")
        out({"systemMessage": f"Precedent hook error (fail-open): {e}"})


if __name__ == "__main__":
    main()
