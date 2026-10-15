"""Read side: what agents (MCP), hooks and the CLI ask. Every answer is short plain text with ids and sources.

Token budget matters more than completeness: an agent sees these lines inside its own context.
"""

import fnmatch
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .scan import forbid_tokens

VERDICT = {"human": "deny", "documented": "deny", "agent": "ask", "historical": "ask", "observed": "note"}
RANK = {"ok": 0, "note": 1, "ask": 2, "deny": 3}
MANIFEST = re.compile(r"(requirements[^/]*\.txt|pyproject\.toml|package\.json|go\.mod)$")


def day(ms):
    return datetime.fromtimestamp((ms or 0) / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else "?"


def now_ms():
    return int(time.time() * 1000)


def rel(path):
    """Repo-relative, forward slashes."""
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(Path(config.REPO).resolve())
        except ValueError:
            pass
    return p.as_posix()


# ---------------------------------------------------------------- lookups

_components = None


def component_of(path):
    r = db.read(db.q("component_of_file"), path=path)
    if r:
        return r[0][0]
    global _components  # new file: longest folder prefix among known components
    if _components is None:
        _components = [c for (c,) in db.read("MATCH (c:Component) RETURN c.name")]
    best = "."
    for c in _components:
        if path.startswith(c + "/") and len(c) > len(best.strip(".")):
            best = c
    return best


def governing(path):
    """Active decisions that apply to a file: its component's, repo-wide ones, and path-scoped ones matching it."""
    cols = ["id", "title", "rationale", "trust", "version", "forbid", "paths", "source", "since", "locked", "chose", "rejected"]
    rows = [dict(zip(cols, r)) for r in db.read(db.q("governing"), component=component_of(path))]
    rows += [dict(zip(cols, r)) for r in db.read(db.q("path_scoped"))]
    rows = [d for d in rows if not d["paths"] or any(fnmatch.fnmatch(path, g) for g in d["paths"])]
    seen, out = set(), []
    for d in rows:
        if d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)
    return out


def verdict_of(d):
    return "deny" if d.get("locked") else VERDICT.get(d["trust"], "note")


# ---------------------------------------------------------------- check: the guard

def violates(token, path, added):
    """Does the added text break this forbid entry?
    'lib:x'  -> x newly imported, required, or added to a manifest
    'any'    -> any edit at all to a governed path (e.g. hand-written migrations)
    other    -> plain substring"""
    if token == "any":
        return True
    if token.startswith("lib:"):
        name = re.escape(token[4:])
        if MANIFEST.search(path):
            pat = rf"^\s*[\"']?{name}(?![\w-])"
        else:
            pat = rf"^\s*(?:import|from)\s+{name}\b|require\(\s*[\"']{name}[\"'/]|from\s+[\"']{name}[\"'/]"
        return re.search(pat, added, re.I | re.M) is not None
    return token in added


def added_text(new_text, old_text):
    old = set((old_text or "").splitlines())
    return "\n".join(l for l in (new_text or "").splitlines() if l not in old)


def check(path, new_text, old_text=None):
    """Would this edit break a decision? -> {'verdict': ok|note|ask|deny, 'hits': [...], 'text': '...'}"""
    if config.MODE == "off":
        return {"verdict": "ok", "hits": [], "text": "Precedent is off."}
    path = rel(path)
    if old_text is None:
        f = Path(config.REPO) / path
        old_text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    added = added_text(new_text, old_text)
    hits = []
    for d in governing(path):
        for tok in d["forbid"] or []:
            if violates(tok, path, added):
                hits.append({**d, "token": tok, "verdict": verdict_of(d)})
                break
    if not hits:
        return {"verdict": "ok", "hits": [], "text": ""}
    worst = max((h["verdict"] for h in hits), key=RANK.get)
    lines = [f"Precedent: this edit to {path} conflicts with {len(hits)} decision(s):"]
    for h in sorted(hits, key=lambda h: -RANK[h["verdict"]])[:4]:
        what = "edits to this path" if h["token"] == "any" else h["token"].removeprefix("lib:")
        lines.append(f"- {h['id']} v{h['version']} [{h['trust']}] {h['title']} (since {day(h['since'])}, "
                     f"source {h['source']}). Forbids: {what}. Why: {h['rationale'][:220]}")
    if worst == "deny":
        lines.append("Do not work around this. Tell the user. Only a human can change this decision.")
    elif worst == "ask":
        lines.append("If this is intentional, the user can approve; then record why with "
                     f"supersede('{hits[0]['id']}', expected_version={hits[0]['version']}, title, rationale).")
    return {"verdict": worst, "hits": hits, "text": "\n".join(lines)}


# ---------------------------------------------------------------- context: what an agent sees when it reads a file

def relevant(d, text):
    """Library decisions only matter to a file that mentions the library (old or new one). Others always do."""
    libs = (d.get("rejected") or []) + (d.get("chose") or [])
    if not libs or d["paths"]:
        return True
    names = {t.removeprefix("lib:") for l in libs for t in forbid_tokens(l, ())}
    return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I) for n in names)


def context(paths, skip_ids=(), max_decisions=5):
    """Decisions and notes for these files, tiny. Returns (text, decision_ids_shown). Empty text = nothing to say."""
    lines, shown = [], []
    for path in [rel(p) for p in paths][:5]:
        f = Path(config.REPO) / path
        text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        all_ds = [d for d in governing(path) if d["id"] not in skip_ids and d["id"] not in shown]
        ds = sorted((d for d in all_ds if relevant(d, text)), key=lambda d: (-RANK[verdict_of(d)], -(d["since"] or 0)))
        notes = db.read("MATCH (:File {path: $p})-[:HAS_NOTE]->(n:Note) RETURN n.line, n.kind, n.text LIMIT 3", p=path)
        if not ds and not notes:
            continue
        lines.append(f"Precedent for {path} (component {component_of(path)}):")
        for d in ds[:max_decisions]:
            chose = f" -> use {', '.join(d['chose'])}" if d.get("chose") else ""
            lines.append(f"- {d['id']} v{d['version']} [{d['trust']} -> {verdict_of(d)}] {d['title']}{chose} ({d['source']})")
            shown.append(d["id"])
        for line, kind, text in notes:
            lines.append(f"- note L{line} [{kind}] {text[:140]}")
    return "\n".join(lines), shown


# ---------------------------------------------------------------- questions

def impact(target):
    comp = target if db.read("MATCH (c:Component {name: $n}) RETURN c", n=target) else component_of(rel(target))
    rows = db.read(db.q("impact"), component=comp)
    if not rows:
        return f"Nothing depends on {comp} (from imports)."
    out = [f"{len(rows)} components depend on {comp} (current imports, up to 3 hops):"]
    for name, hops, ds in rows:
        out.append(f"- {name} ({hops} hop{'s' if hops > 1 else ''})" + (f", decisions: {', '.join(ds[:4])}" if ds else ""))
    return "\n".join(out)


def who_knows(target):
    comp = target if db.read("MATCH (c:Component {name: $n}) RETURN c", n=target) else component_of(rel(target))
    rows = db.read(db.q("who_knows"), component=comp)
    owners = db.read("MATCH (t:Team)-[:OWNS]->(:Component {name: $c}) OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t) "
                     "RETURN t.name, collect(p.name)", c=comp)
    out = [f"Who knows {comp}:"]
    out += [f"- owner team {t}: {', '.join(ms[:5])}" for t, ms in owners]
    out += [f"- {n} <{e}>: {c} commits, last {day(l)}" for n, e, c, l in rows]
    return "\n".join(out)


def _decision_line(d):
    p = d.properties
    end = f" until {day(p['valid_to'])}" if p.get("valid_to") else ""
    return (f"{p['id']} v{p.get('version', 1)} [{p.get('status')}, {p.get('trust')}] {p.get('title')} "
            f"(from {day(p.get('valid_from'))}{end}, by {p.get('actor')}, source {p.get('source')})")


def why(target, as_of=None):
    """A decision id (D-12), a library name, or a topic. as_of='YYYY-MM-DD' answers for that date."""
    if as_of:
        t = int(datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        comp = target if db.read("MATCH (c:Component {name: $n}) RETURN c", n=target) else "*"
        rows = db.read(db.q("as_of"), t=t, component=comp)
        if comp == "*":
            rows = [r for r in rows if target.lower() in r[1].lower()] or rows[:10]
        return f"In force on {as_of}:\n" + "\n".join(f"- {i} [{tr}] {ti} (from {day(f)})" for i, ti, tr, f, _ in rows)
    if re.fullmatch(r"[DP]-\d+", target):
        rows = db.read(db.q("lineage"), id=target)
        if not rows:
            return f"No decision {target}."
        d, older, newer = rows[0]
        out = [_decision_line(d), f"  why: {d.properties.get('rationale', '')[:400]}"]
        for n in newer:
            out.append(f"  superseded by: {_decision_line(n)}")
        for o in older:
            out.append(f"  replaced: {_decision_line(o)}")
        for cid, op, actor, at, reason in db.read(
                "MATCH (ch:Change)-[:ON]->(:Decision {id: $id}) RETURN ch.id, ch.op, ch.actor, ch.at, ch.reason "
                "ORDER BY ch.at", id=target):
            out.append(f"  {cid} {day(at)} {op} by {actor}: {reason[:100]}")
        return "\n".join(out)
    lib = db.read("MATCH (l:Library {name: $n}) OPTIONAL MATCH (d:Decision)-[r:CHOSE|REJECTED]->(l) "
                  "RETURN l.why, l.present, l.since, l.added_in, collect(d.id)", n=target.lower())
    if lib:
        w, present, since, added_in, ds = lib[0]
        state = "in use" if present else "no longer in the manifests"
        return (f"Library {target}: {state}. Added {day(since)} ({added_in or 'before history'}). "
                f"Why: {w or 'no comment in the manifest'}. Decisions: {', '.join(ds) or 'none'}.")
    return search(target)


def search(text):
    q = " ".join(re.findall(r"\w+", text))
    rows = db.read(db.q("search"), q=q) if q else []
    if not rows:
        return f"No decisions match '{text}'."
    return "\n".join(f"- {i} [{s}, {t}] {ti}" for i, ti, s, t, _ in rows)


def history(since_ms=0, exclude_session="", limit=15):
    rows = db.read(db.q("history"), since=since_ms, exclude=exclude_session, limit=limit)
    if not rows:
        return "No changes."
    return "\n".join(f"- {cid} {day(at)} {actor} ({h}) {op} {', '.join(ds)}: {reason[:90]}"
                     for cid, at, op, actor, h, reason, ds in rows)


def query(cypher, limit=50):
    """Read-only Cypher for power users and agents. Writes are refused by FalkorDB (ro_query); LIMIT is forced."""
    if not re.search(r"\blimit\b", cypher, re.I):
        cypher += f" LIMIT {limit}"
    rows = db.read(cypher)[:limit]
    return "\n".join(" | ".join(str(c) for c in r) for r in rows) or "(no rows)"


def pending():
    rows = db.read("MATCH (p:Decision {status: 'proposed'}) OPTIONAL MATCH (p)-[:PROPOSES_TO_SUPERSEDE]->(o:Decision) "
                   "RETURN p.id, p.title, p.actor, o.id, o.title, p.recorded_at ORDER BY p.recorded_at")
    return [{"id": i, "title": t, "actor": a, "replaces": o, "replaces_title": ot, "at": at} for i, t, a, o, ot, at in rows]


def stats():
    labels = ["Component", "File", "Library", "Person", "Decision", "Note", "Session", "Change", "Commit"]
    return {l: db.read(f"MATCH (n:{l}) RETURN count(n)")[0][0] for l in labels}
