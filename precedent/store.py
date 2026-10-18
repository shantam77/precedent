"""Write side: every change to a decision goes through here.

Four rules keep a shared graph safe when agents write to it:
  1. never delete or overwrite: supersede/retire set status + valid_to, and the old node stays
  2. every write is a Change node (who, which harness/session, what, before/after) -> revert() can undo it
  3. optimistic concurrency: writes to an existing decision carry the version the writer read (check-and-set)
  4. policy(): agents change low-trust things directly; anything protected becomes a proposal a human approves
"""

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, db, tools

TRUST = ["observed", "historical", "agent", "documented", "human"]


@dataclass
class Actor:
    email: str
    kind: str          # "agent" | "human"
    harness: str       # claude-code | cursor | cli | sim
    session: str


def me(kind=None, session="cli"):
    harness = config.HARNESS
    return Actor(config.actor_email(), kind or ("human" if harness == "cli" else "agent"), harness, session)


def result(ok, status, text, **extra):
    return {"ok": ok, "status": status, "text": text, **extra}


# ---------------------------------------------------------------- policy: the one place that decides access

def policy(actor, op, target=None, want_trust=None):
    """-> 'apply' | 'approval' | 'owner' | 'refuse: why'
    op: record | supersede | retire | amend | approve | revert"""
    if op == "record":
        if actor.kind == "agent" and want_trust in ("documented", "human"):
            return "approval"
        return "apply"
    if op in ("supersede", "retire"):
        if target["locked"]:
            return "owner"
        if actor.kind == "human" or TRUST.index(target["trust"]) <= TRUST.index("agent"):
            return "apply"
        return "approval"
    if op == "amend":
        if actor.kind == "human" or target["actor"] == actor.email or TRUST.index(target["trust"]) <= TRUST.index("agent"):
            return "apply"
        return "refuse: you can only amend your own decisions"
    if op == "approve":
        if actor.kind == "human":
            return "apply"
        return "refuse: only a human can approve"
    return "refuse: unknown operation"


# ---------------------------------------------------------------- small helpers

def next_id(counter, prefix):
    n = db.write("MERGE (c:Counter {name: $n}) SET c.n = coalesce(c.n, 0) + 1 RETURN c.n", n=counter)[0][0]
    return f"{prefix}-{n}"


def ensure_session(actor):
    """Person (matched by email) -RAN-> Session. Returns the session's write count so far."""
    key = db.read("MATCH (p:Person) WHERE $e IN p.emails RETURN p.key LIMIT 1", e=actor.email)
    key = key[0][0] if key else "e:" + actor.email
    rows = db.write("""MERGE (p:Person {key: $key}) ON CREATE SET p.name = $e, p.emails = [$e]
        MERGE (s:Session {id: $sid}) ON CREATE SET s.harness = $h, s.actor = $e, s.kind = $kind, s.started_at = timestamp(), s.writes = 0
        MERGE (p)-[:RAN]->(s) RETURN s.writes""", key=key, e=actor.email, sid=actor.session, h=actor.harness, kind=actor.kind)
    return rows[0][0]


def log_change(actor, op, reason, on=(), before=None, after=None):
    cid = next_id("C", "C")
    db.write("""MATCH (s:Session {id: $sid})
        CREATE (s)-[:MADE]->(ch:Change {id: $cid, op: $op, at: timestamp(), actor: $e, kind: $kind, harness: $h,
                                        session: $sid, reason: $reason, before: $before, after: $after})
        SET s.writes = s.writes + 1
        WITH ch UNWIND $on AS did MATCH (d:Decision {id: did}) CREATE (ch)-[:ON]->(d)""",
             sid=actor.session, cid=cid, op=op, e=actor.email, kind=actor.kind, h=actor.harness,
             reason=reason[:500], before=json.dumps(before or {}), after=json.dumps(after or {}), on=list(on))
    return cid


def get(did):
    rows = db.read("""MATCH (d:Decision {id: $id})
        OPTIONAL MATCH (d)-[:ABOUT]->(x) OPTIONAL MATCH (n:Decision)-[:SUPERSEDES]->(d)
        RETURN d, collect(DISTINCT x.name), collect(DISTINCT n.id)""", id=did)
    if not rows:
        return None
    d, about, newer = rows[0]
    p = dict(d.properties)
    p.setdefault("locked", False)
    p.setdefault("paths", [])
    p.setdefault("forbid", [])
    return {**p, "about": about, "superseded_by": newer}


def stale_text(d, expected):
    by = f", superseded by {', '.join(d['superseded_by'])}" if d["superseded_by"] else ""
    last = db.read("MATCH (ch:Change)-[:ON]->(:Decision {id: $id}) RETURN ch.actor, ch.harness, ch.at, ch.op "
                   "ORDER BY ch.at DESC LIMIT 1", id=d["id"])
    who = f" Last change: {last[0][3]} by {last[0][0]} ({last[0][1]}) on {tools.day(last[0][2])}." if last else ""
    return (f"{d['id']} changed since you read it: you have v{expected}, it is now v{d['version']} "
            f"[{d['status']}]{by}.{who} Re-read it with why('{d['id']}') and decide again.")


def check_evidence(evidence):
    """Cheap verification: file:line must exist in the repo, commit:sha must exist in git. Other kinds are taken as given."""
    bad = []
    for ev in evidence:
        m = re.fullmatch(r"commit:([0-9a-f]{6,40})", ev)
        if m:
            ok = subprocess.run(["git", "cat-file", "-e", m.group(1) + "^{commit}"], cwd=config.REPO,
                                capture_output=True).returncode == 0
        elif re.match(r"^(user|url|issue|pr|chat):", ev):
            ok = True
        else:
            path, _, line = ev.removeprefix("file:").partition(":")  # agents write "file:path:line" too
            f = Path(config.REPO) / path
            ok = f.is_file() and (not line.isdigit() or int(line) <= len(f.read_text(encoding="utf-8", errors="replace").splitlines()))
        if not ok:
            bad.append(ev)
    return bad


def scope(paths, components):
    """ABOUT targets: the given components, plus for each path glob the component that contains its fixed prefix
    and every component below that prefix ("netbox/*" covers netbox/dcim too). No prefix ("*/migrations/*") = repo."""
    comps = set(components)
    for p in paths:
        prefix = re.split(r"[*?\[]", p)[0].rstrip("/")
        if prefix:
            comps.add(tools.component_of(prefix))
            comps |= {c for (c,) in db.read("MATCH (c:Component) WHERE c.name = $p OR c.name STARTS WITH $ps RETURN c.name", p=prefix, ps=prefix + "/")}
    comps.discard(".")
    return sorted(comps)


def words(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def similar_active(title, chose, rejected, comps, paths):
    """Active decisions that this new one duplicates or contradicts (same scope)."""
    rows = db.read("""MATCH (x:Component) WHERE x.name IN $comps
        WITH x MATCH (x)<-[:ABOUT]-(d:Decision) WHERE d.status = 'active'
        RETURN d.id AS id, d.title AS title, d.version AS v, d.trust AS trust, d.paths AS paths,
               [(d)-[:CHOSE]->(c1:Library) | c1.name] AS chose, [(d)-[:REJECTED]->(r1:Library) | r1.name] AS rej
        UNION
        MATCH (x:Repo) WITH x MATCH (x)<-[:ABOUT]-(d:Decision) WHERE d.status = 'active'
        RETURN d.id AS id, d.title AS title, d.version AS v, d.trust AS trust, d.paths AS paths,
               [(d)-[:CHOSE]->(c2:Library) | c2.name] AS chose, [(d)-[:REJECTED]->(r2:Library) | r2.name] AS rej""", comps=comps)
    dup, conflict = [], []
    for did, t, v, trust, dpaths, dchose, drej in rows:
        if dpaths and paths and not any(fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a) or a == b
                                        for a in paths for b in dpaths):
            continue
        if set(chose) & set(drej) or set(rejected) & set(dchose):
            conflict.append((did, t, v, trust))
        else:
            a, b = words(title), words(t)
            if (a and len(a & b) / len(a | b) >= 0.6) or (chose and set(chose) == set(dchose)):
                dup.append((did, t, v, trust))
    return dup, conflict


# ---------------------------------------------------------------- writes

def _create(actor, status, trust, title, rationale, paths, comps, chose, rejected, forbid, evidence, bad_evidence):
    did = next_id("D", "D")
    # rejected libraries are banned automatically; prose an agent put in `rejected` is not a library name
    forbid = sorted(set(forbid) | {f"lib:{r}" for r in rejected if re.fullmatch(r"@?[a-z0-9][\w.\-/]{0,60}", r)})
    paths = [p + "*" if p.endswith("/") else p for p in paths]  # "netbox/*/migrations/" means everything below
    db.write("""CREATE (d:Decision {id: $id, title: $title, rationale: $rationale, status: $status, trust: $trust,
                locked: false, version: 1, forbid: $forbid, paths: $paths, recorded_at: timestamp(),
                actor: $e, session: $sid, harness: $h, source: $source, evidence_ok: $ok})""",
             id=did, title=title, rationale=rationale, status=status, trust=trust, forbid=forbid, paths=paths,
             e=actor.email, sid=actor.session, h=actor.harness, source=", ".join(evidence) or f"{actor.harness} session",
             ok=not bad_evidence)
    if status == "active":
        db.write("MATCH (d:Decision {id: $id}) SET d.valid_from = timestamp()", id=did)
    if comps:
        db.write("MATCH (d:Decision {id: $id}) UNWIND $comps AS c MATCH (x:Component {name: c}) CREATE (d)-[:ABOUT]->(x)",
                 id=did, comps=comps)
    else:
        db.write("MATCH (d:Decision {id: $id}), (r:Repo) CREATE (d)-[:ABOUT]->(r)", id=did)
    for rel, libs in (("CHOSE", chose), ("REJECTED", rejected)):
        if libs:
            db.write(f"MATCH (d:Decision {{id: $id}}) UNWIND $libs AS l MERGE (lib:Library {{name: l}}) "
                     f"ON CREATE SET lib.present = false, lib.why = '', lib.since = 0, lib.added_in = 'recorded' "
                     f"CREATE (d)-[:{rel}]->(lib)", id=did, libs=[l.lower() for l in libs])
    db.write("MATCH (d:Decision {id: $id}), (s:Session {id: $sid}) CREATE (s)-[:RECORDED]->(d)", id=did, sid=actor.session)
    return did


def _budget_ok(actor):
    writes = ensure_session(actor)
    return actor.kind == "human" or writes < config.WRITE_BUDGET


def record_decision(actor, title, rationale, paths=(), components=(), chose=(), rejected=(), forbid=(),
                    evidence=(), trust=None, supersedes=None, expected_version=None, force_new=False):
    """Create a decision. To replace one, pass supersedes + expected_version (or call supersede())."""
    if supersedes:
        return supersede(actor, supersedes, expected_version, title=title, rationale=rationale, paths=paths,
                         components=components, chose=chose, rejected=rejected, forbid=forbid, evidence=evidence)
    if not _budget_ok(actor):
        return result(False, "refused", f"Write budget of {config.WRITE_BUDGET} per session used up. Ask the user.")
    if len(title.strip()) < 5 or len(rationale.strip()) < 10:
        return result(False, "refused", "Give a title and a real rationale (why, not what).")
    paths, chose, rejected = list(paths), [c.lower() for c in chose], [r.lower() for r in rejected]
    comps = scope(paths, components)
    if actor.kind == "agent" and not (paths or comps):
        return result(False, "refused", "Say what this is about: pass paths (globs ok) or components.")
    dup, conflict = similar_active(title, chose, rejected, comps, paths)
    if conflict:
        lines = "\n".join(f"- {d} v{v} [{t}] {ti}" for d, ti, v, t in conflict)
        return result(False, "conflict", f"This contradicts active decision(s):\n{lines}\n"
                      f"If it should replace one, call supersede(id, expected_version, ...).", conflicts=[c[0] for c in conflict])
    if dup and not force_new:
        lines = "\n".join(f"- {d} v{v} [{t}] {ti}" for d, ti, v, t in dup)
        return result(False, "duplicate", f"Looks like an existing decision:\n{lines}\n"
                      f"Use amend() to add to it, supersede() to replace it, or force_new=true if it's really different.",
                      duplicates=[d[0] for d in dup])
    bad = check_evidence(evidence)
    want = trust or ("human" if actor.kind == "human" else "agent")
    decision = policy(actor, "record", want_trust=want)
    status = "active" if decision == "apply" else "proposed"
    did = _create(actor, status, want, title, rationale, paths, comps, chose, rejected, forbid, list(evidence), bad)
    cid = log_change(actor, "record" if status == "active" else "propose", rationale, on=[did])
    note = f" Unverified evidence: {', '.join(bad)}." if bad else ""
    if status == "proposed":
        return result(True, "pending", f"{did} recorded as a PROPOSAL ({want}); a human must approve it.{note}", id=did, change=cid)
    return result(True, "applied", f"{did} recorded [{want}] about {', '.join(comps) or 'the whole repo'} ({cid}).{note}",
                  id=did, change=cid)


def _end(actor, old, expected_version, new_status, op, reason, new_fields=None):
    """Supersede or retire `old`, through policy and a check-and-set on its version."""
    if not _budget_ok(actor):
        return result(False, "refused", f"Write budget of {config.WRITE_BUDGET} per session used up. Ask the user.")
    d = get(old)
    if not d:
        return result(False, "refused", f"No decision {old}.")
    if expected_version is None:
        return result(False, "refused", f"Pass expected_version (the version you read: {old} is v{d['version']}).")
    if d["status"] != "active" or d["version"] != int(expected_version):
        return result(False, "stale", stale_text(d, expected_version))
    decision = policy(actor, op, d)
    if decision.startswith("refuse"):
        return result(False, "refused", decision)
    f = dict(new_fields or {})
    if op == "supersede":
        f.setdefault("paths", d["paths"])
        f["components"] = list(f.get("components") or []) or [a for a in d["about"] if a != db.read("MATCH (r:Repo) RETURN r.name")[0][0]]
    if decision in ("approval", "owner"):
        title = f.get("title") or f"Retire {old}: {reason[:80]}"
        did = _create(actor, "proposed", "human" if actor.kind == "human" else "agent", title, f.get("rationale") or reason,
                      list(f.get("paths") or []), scope(list(f.get("paths") or []), f.get("components") or []),
                      [c.lower() for c in f.get("chose") or []], [r.lower() for r in f.get("rejected") or []],
                      f.get("forbid") or [], list(f.get("evidence") or []), [])
        db.write("MATCH (p:Decision {id: $p}), (o:Decision {id: $o}) CREATE (p)-[:PROPOSES {op: $op, expected_version: $v}]->(o)",
                 p=did, o=old, op=op, v=d["version"])
        cid = log_change(actor, "propose", reason, on=[did, old])
        who = "an owner-team member" if decision == "owner" else "a human"
        return result(True, "pending", f"{did} proposes to {op} {old} [{d['trust']}{', locked' if d['locked'] else ''}]. "
                      f"{old} stays in force until {who} approves ({cid}).", id=did, change=cid)
    return _apply_end(actor, d, new_status, op, reason, f)


def _apply_end(actor, d, new_status, op, reason, f, approved_proposal=None):
    rows = db.write("""MATCH (o:Decision {id: $id}) WHERE o.version = $v AND o.status = 'active'
        SET o.status = $st, o.valid_to = timestamp(), o.version = o.version + 1 RETURN o.id""",
                    id=d["id"], v=d["version"], st=new_status)
    if not rows:  # someone else won the race between our read and this write
        return result(False, "stale", stale_text(get(d["id"]), d["version"]))
    new = None
    if op == "supersede":
        if approved_proposal:
            new = approved_proposal
            db.write("MATCH (n:Decision {id: $n}) SET n.status = 'active', n.valid_from = timestamp()", n=new)
        else:
            new = _create(actor, "active", "human" if actor.kind == "human" else "agent", f["title"], f["rationale"],
                          list(f.get("paths") or []), scope(list(f.get("paths") or []), f.get("components") or []),
                          [c.lower() for c in f.get("chose") or []], [r.lower() for r in f.get("rejected") or []],
                          f.get("forbid") or [], list(f.get("evidence") or []), check_evidence(f.get("evidence") or []))
        db.write("MATCH (n:Decision {id: $n}), (o:Decision {id: $o}) CREATE (n)-[:SUPERSEDES {at: timestamp()}]->(o)",
                 n=new, o=d["id"])
    elif approved_proposal:
        db.write("MATCH (n:Decision {id: $n}) SET n.status = 'applied'", n=approved_proposal)
    on = [d["id"]] + ([new] if new else [])
    cid = log_change(actor, op, reason, on=on, before={"id": d["id"], "version": d["version"], "status": "active"},
                     after={"id": d["id"], "status": new_status, "new": new})
    text = f"{d['id']} {new_status}" + (f", replaced by {new}" if new else "") + f" ({cid}). Revert with revert('{cid}')."
    return result(True, "applied", text, id=new or d["id"], change=cid)


def supersede(actor, old, expected_version, title, rationale, paths=(), components=(), chose=(), rejected=(),
              forbid=(), evidence=()):
    if len(title.strip()) < 5 or len(rationale.strip()) < 10:
        return result(False, "refused", "Give a title and a real rationale (why, not what).")
    return _end(actor, old, expected_version, "superseded", "supersede", rationale,
                dict(title=title, rationale=rationale, paths=list(paths), components=list(components), chose=chose,
                     rejected=rejected, forbid=list(forbid), evidence=list(evidence)))


def retire(actor, old, expected_version, reason):
    return _end(actor, old, expected_version, "retired", "retire", reason)


def amend(actor, did, expected_version, rationale=None, add_paths=(), add_forbid=(), add_evidence=()):
    if not _budget_ok(actor):
        return result(False, "refused", f"Write budget of {config.WRITE_BUDGET} per session used up. Ask the user.")
    d = get(did)
    if not d:
        return result(False, "refused", f"No decision {did}.")
    if d["version"] != int(expected_version) or d["status"] not in ("active", "proposed"):
        return result(False, "stale", stale_text(d, expected_version))
    decision = policy(actor, "amend", d)
    if decision.startswith("refuse"):
        return result(False, "refused", decision)
    new = {"rationale": rationale or d["rationale"], "paths": sorted(set(d["paths"]) | set(add_paths)),
           "forbid": sorted(set(d["forbid"]) | set(add_forbid)),
           "source": ", ".join(filter(None, [d.get("source", "")] + list(add_evidence)))}
    rows = db.write("""MATCH (d:Decision {id: $id}) WHERE d.version = $v
        SET d.rationale = $r, d.paths = $p, d.forbid = $f, d.source = $s, d.version = d.version + 1 RETURN d.version""",
                    id=did, v=d["version"], r=new["rationale"], p=new["paths"], f=new["forbid"], s=new["source"])
    if not rows:
        return result(False, "stale", stale_text(get(did), expected_version))
    before = {k: d.get(k) for k in new} | {"version": d["version"]}
    cid = log_change(actor, "amend", rationale or "amend", on=[did], before=before, after=new)
    return result(True, "applied", f"{did} amended, now v{rows[0][0]} ({cid}).", id=did, change=cid)


def approve(actor, pid):
    decision = policy(actor, "approve")
    if decision.startswith("refuse"):
        return result(False, "refused", decision)
    ensure_session(actor)
    p = get(pid)
    if not p or p["status"] != "proposed":
        return result(False, "refused", f"{pid} is not a pending proposal.")
    target = db.read("MATCH (:Decision {id: $p})-[r:PROPOSES]->(o:Decision) RETURN o.id, r.op, r.expected_version", p=pid)
    if not target:  # a proposed new decision (e.g. an agent asking for 'documented' trust)
        db.write("MATCH (d:Decision {id: $id}) SET d.status = 'active', d.valid_from = timestamp(), d.version = d.version + 1",
                 id=pid)
        cid = log_change(actor, "approve", f"approved {pid}", on=[pid])
        return result(True, "applied", f"{pid} approved and active ({cid}).", id=pid, change=cid)
    old, op, v = target[0]
    d = get(old)
    if d["locked"]:
        owners = db.read("""MATCH (:Decision {id: $id})-[:ABOUT]->(:Component)<-[:OWNS]-(t:Team)
            OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t) WHERE $e IN p.emails RETURN t.name, count(p)""", id=old, e=actor.email)
        if owners and not any(n for _, n in owners):
            return result(False, "refused", f"{old} is locked: only members of {', '.join(t for t, _ in owners)} can approve.")
    if d["status"] != "active" or d["version"] != v:
        db.write("MATCH (p:Decision {id: $p}) SET p.status = 'rejected', p.reason = 'stale'", p=pid)
        return result(False, "stale", f"{pid} is stale and was closed. " + stale_text(d, v))
    return _apply_end(actor, d, "superseded" if op == "supersede" else "retired", op, f"approved {pid}", {},
                      approved_proposal=pid)


def reject(actor, pid, reason):
    if actor.kind != "human":
        return result(False, "refused", "Only a human can reject a proposal.")
    ensure_session(actor)
    rows = db.write("MATCH (d:Decision {id: $id, status: 'proposed'}) SET d.status = 'rejected', d.reason = $r RETURN d.id",
                    id=pid, r=reason)
    if not rows:
        return result(False, "refused", f"{pid} is not a pending proposal.")
    cid = log_change(actor, "reject", reason, on=[pid])
    return result(True, "applied", f"{pid} rejected ({cid}).", id=pid, change=cid)


def revert(actor, cid, reason):
    """Undo one change. Agents may only undo changes from their own session."""
    rows = db.read("MATCH (ch:Change {id: $id}) RETURN ch.op, ch.session, ch.before, ch.after, ch.reverted", id=cid)
    if not rows:
        return result(False, "refused", f"No change {cid}.")
    op, session, before, after, reverted = rows[0]
    if reverted:
        return result(False, "refused", f"{cid} was already reverted.")
    if actor.kind == "agent" and session != actor.session:
        return result(False, "refused", f"{cid} was made in another session; ask a human to revert it.")
    ensure_session(actor)
    before, after = json.loads(before or "{}"), json.loads(after or "{}")
    on = [i for (i,) in db.read("MATCH (:Change {id: $id})-[:ON]->(d:Decision) RETURN d.id", id=cid)]
    if op in ("record", "propose"):
        db.write("MATCH (d:Decision {id: $id}) WHERE d.status IN ['active', 'proposed'] "
                 "SET d.status = 'reverted', d.valid_to = timestamp(), d.version = d.version + 1", id=on[0])
    elif op in ("supersede", "retire", "approve"):
        old, new = after.get("id"), after.get("new")
        if old:
            db.write("MATCH (o:Decision {id: $o}) SET o.status = 'active', o.valid_to = NULL, o.version = o.version + 1", o=old)
        if new:
            db.write("MATCH (n:Decision {id: $n}) SET n.status = 'reverted', n.valid_to = timestamp(), n.version = n.version + 1", n=new)
    elif op == "amend":
        db.write("MATCH (d:Decision {id: $id}) SET d.rationale = $r, d.paths = $p, d.forbid = $f, d.source = $s, "
                 "d.version = d.version + 1", id=on[0], r=before["rationale"], p=before["paths"], f=before["forbid"],
                 s=before["source"])
    else:
        return result(False, "refused", f"Can't revert a '{op}' change.")
    db.write("MATCH (ch:Change {id: $id}) SET ch.reverted = true", id=cid)
    rc = log_change(actor, "revert", f"revert {cid}: {reason}", on=on)
    return result(True, "applied", f"{cid} reverted ({rc}).", id=cid, change=rc)


# ---------------------------------------------------------------- teams (humans only, via CLI)

def team(name, members=(), owns=()):
    db.write("MERGE (t:Team {name: $n})", n=name)
    for email in members:
        db.write("MATCH (t:Team {name: $n}) MATCH (p:Person) WHERE $e IN p.emails MERGE (p)-[:MEMBER_OF]->(t)", n=name, e=email)
    for comp in owns:
        db.write("MATCH (t:Team {name: $n}), (c:Component {name: $c}) MERGE (t)-[:OWNS]->(c)", n=name, c=comp)


def lock(actor, did, locked=True):
    if actor.kind != "human":
        return result(False, "refused", "Only a human can lock a decision.")
    ensure_session(actor)
    db.write("MATCH (d:Decision {id: $id}) SET d.locked = $l, d.version = d.version + 1", id=did, l=locked)
    cid = log_change(actor, "lock" if locked else "unlock", "", on=[did])
    return result(True, "applied", f"{did} {'locked' if locked else 'unlocked'} ({cid}).", id=did, change=cid)
