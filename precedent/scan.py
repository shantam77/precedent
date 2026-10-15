"""Cold start: build the graph from what every repo already has. No LLM, no docs required.

    python -m precedent init <repo>          (see cli.py)

What it reads, cheapest first:
  1. git ls-files            -> Components (folders) and Files
  2. imports (Python AST)    -> Component DEPENDS_ON Component, Component USES Library
  3. manifests at HEAD       -> Library, with the comment written above it ("why")
  4. manifest history        -> Library removed / replaced  => historical Decision (with the commit as evidence)
  5. git log --name-only     -> Person KNOWS Component (aliases merged by name)
  6. agent docs + comments   -> Note (rules like "never write migrations manually"), with file:line
"""

import ast
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import db

CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go"}
PY_MANIFESTS = ("requirements.txt", "base_requirements.txt", "requirements_testing.txt", "pyproject.toml")
DOC_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", ".cursorrules")
ADR_DIRS = ("docs/adr", "doc/adr", "docs/decisions", "docs/architecture/decisions", "adr")
RULE_WORDS = re.compile(r"\b(never|do not|don't|must not|must|always|instead of|avoid|not pytest)\b", re.I)
NOTE_MARK = re.compile(r"(?:#|//)\s*(NOTE|IMPORTANT|WARNING|DO NOT|DON'T|NEVER|WHY|DECISION|HACK)\b[:\s-]*(.+)")
REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[<>=!~;\[@].*)?$")
REPLACED = re.compile(r"(?:replace|replacing)\s+([\w.-]+)\s+(?:with|by)\s+([\w.-]+)|(?:migrate|move|switch)\s+from\s+([\w.-]+)\s+to\s+([\w.-]+)", re.I)
STOP_TOKENS = {"html", "just", "slim", "core", "types", "django", "python", "flask", "swagger", "cookie", "wheel"}
SEP = "\x00"   # field separator in our git log formats (%x00)
REC = "\x01"   # record separator (%x01)


def git(repo, *args):
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout


def pep503(name):
    return re.sub(r"[-_.]+", "-", name.lower())


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ---------------------------------------------------------------- 1. components

def components(files, max_share=0.25):
    """Group files by folder. A folder holding more than max_share of all files is split one level deeper."""
    total = len(files)

    def group(paths, depth):
        buckets = defaultdict(list)
        for p in paths:
            parts = p.split("/")
            key = "/".join(parts[:depth + 1]) if len(parts) > depth + 1 else "/".join(parts[:depth])
            buckets[key or "."].append(p)
        out = {}
        for key, ps in buckets.items():
            has_sub = any(len(p.split("/")) > depth + 2 for p in ps)
            if len(ps) > max_share * total and has_sub and depth < 3:
                out.update(group(ps, depth + 1))
            else:
                out[key] = ps
        return out

    return group(files, 0)


def component_of(path, comp_paths):
    """Longest component path that is a prefix of this path."""
    best = "."
    for c in comp_paths:
        if (path == c or path.startswith(c + "/")) and len(c) > len(best.strip(".")):
            best = c
    return best


# ---------------------------------------------------------------- 2. imports

def python_packages(files):
    """Top-level importable packages: folders with __init__.py whose parent has none. name -> path."""
    inits = {p.rsplit("/", 1)[0] for p in files if p.endswith("/__init__.py")}
    tops = {}
    for d in inits:
        parent = d.rsplit("/", 1)[0] if "/" in d else ""
        if parent not in inits:
            tops[d.rsplit("/", 1)[-1]] = d
    return tops


def py_imports(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.append(node.module)
    return mods


def scan_imports(repo, files, comp_paths):
    tops = python_packages(files)
    stdlib = set(sys.stdlib_module_names)
    deps, uses = Counter(), defaultdict(set)
    for f in files:
        if not f.endswith(".py"):
            continue
        src = component_of(f, comp_paths)
        text = (repo / f).read_text(encoding="utf-8", errors="replace")
        for mod in py_imports(text):
            top, rest = mod.split(".")[0], mod.split(".")[1:]
            if top in tops:
                dst = component_of("/".join([tops[top], *rest]), comp_paths)
                if dst != src:
                    deps[(src, dst)] += 1
            elif top not in stdlib and not top.startswith("_"):
                uses[(src, top)].add(f)
    return deps, uses


# ---------------------------------------------------------------- 3+4. manifests and their history

def parse_requirements(text):
    """{package: why-comment} from a requirements file; the why is the comment block right above."""
    pkgs, comment = {}, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            c = s.lstrip("# ").strip()
            if c and not c.startswith("http"):
                comment.append(c)
            continue
        m = REQ_LINE.match(s)
        if m and not s.startswith("-"):
            pkgs[pep503(m.group(1))] = " ".join(comment)[:300]
        comment = []
    return pkgs


def parse_pyproject(text):
    pkgs = {}
    for block in re.findall(r"dependencies\s*=\s*\[(.*?)\]", text, re.S):
        for name in re.findall(r"[\"']([A-Za-z0-9][A-Za-z0-9._-]*)", block):
            pkgs[pep503(name)] = ""
    return pkgs


def parse_package_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return {k.lower(): "" for sec in ("dependencies", "devDependencies") for k in data.get(sec, {})}


def parse_manifest(path, text):
    if path.endswith("package.json"):
        return parse_package_json(text)
    if path.endswith("pyproject.toml"):
        return parse_pyproject(text)
    return parse_requirements(text)


def scope_union(state):
    """Python manifests share one scope (the repo, '.'); each package.json is its own folder's scope."""
    scopes = defaultdict(dict)
    for path, pkgs in state.items():
        scope = path.rsplit("/", 1)[0] if path.endswith("package.json") and "/" in path else "."
        for p, why in pkgs.items():
            scopes[scope][p] = scopes[scope].get(p) or why
    return scopes


def manifest_history(repo, manifests, until=None):
    """Walk the mainline (first-parent) history of the manifests, oldest first, with full file text per commit.
    Returns the add/remove events and the package sets at the end."""
    log = git(repo, "log", "--first-parent", "--diff-merges=first-parent", "--reverse", "-p", "-U999999",
              "--format=%x01%H%x00%at%x00%an%x00%s", "--", *manifests)
    state, events = {}, []
    for entry in log.split(REC)[1:]:
        head, _, body = entry.partition("\n")
        sha, ts, author, subject = head.split(SEP, 3)
        ts = int(ts) * 1000
        if until and ts > until:
            break
        before = scope_union(state)
        for diff in re.split(r"^diff --git ", body, flags=re.M)[1:]:
            new = re.search(r"^\+\+\+ (?:b/(.+)|/dev/null)$", diff, re.M)
            if not new:
                continue
            if new.group(1) is None:  # file deleted
                old = re.search(r"^--- a/(.+)$", diff, re.M)
                state.pop(old.group(1) if old else "", None)
                continue
            lines = [l[1:] for l in diff.split("\n") if l[:1] in (" ", "+") and not l.startswith("+++")]
            state[new.group(1)] = parse_manifest(new.group(1), "\n".join(lines))
        after = scope_union(state)
        for scope in set(before) | set(after):
            old, now = before.get(scope, {}), after.get(scope, {})
            added, removed = sorted(set(now) - set(old)), sorted(set(old) - set(now))
            if added or removed:
                events.append(dict(sha=sha, ts=ts, author=author, subject=subject, scope=scope,
                                   added=added, removed=removed, why={p: old[p] for p in removed}))
    return events, scope_union(state)


def forbid_tokens(pkg, present):
    """Library names that, if newly imported or added to a manifest, mean 'using pkg again'.
    Stored as 'lib:<name>' so check() only matches them in import / manifest context."""
    toks = {pkg, pkg.replace("-", "_")}
    first, _, rest = pkg.partition("-")
    if rest and not pkg.startswith("@") and len(first) >= 6 and first not in STOP_TOKENS \
            and not any(p != pkg and p.startswith(first) for p in present):
        toks.add(first)
    if first in STOP_TOKENS and len(rest) >= 5 and rest not in STOP_TOKENS:
        toks.add(rest.replace("-", "_"))
    return ["lib:" + t for t in sorted(toks)]


def import_matches(mod, pkg):
    """Does import name `mod` belong to package `pkg`? (django_filters -> django-filter, rest_framework ->
    djangorestframework, strawberry -> strawberry-graphql; but graphene_django is NOT django)."""
    a, b = norm(mod), norm(pkg)
    return len(a) >= 3 and (a == b or b.startswith(a) or (a.startswith(b) and len(a) - len(b) <= 2)
                            or (len(a) >= 4 and b.endswith(a)))


def related(a, b):
    """Same family of library: psycopg2-binary -> psycopg yes; django-cacheops -> django-redis no."""
    na, nb = norm(a), norm(b)
    na, nb = na.removeprefix("django"), nb.removeprefix("django")
    return len(na) >= 4 and len(nb) >= 4 and (na[:6] == nb[:6] or na in nb or nb in na)


def removal_commit(repo, e, pkg):
    """For a merge/release commit, find the commit inside the merged branch that touched this package's line."""
    if not e["subject"].startswith(("Merge", "Release")):
        return e["sha"], e["subject"]
    pat = re.escape(pkg).replace("\\-", "[-_.]")
    out = git(repo, "log", "--no-merges", "-i", f"-G{pat}", "--format=%H%x00%s", f"{e['sha']}^1..{e['sha']}", "--",
              *PY_MANIFESTS, "*package.json").strip().split("\n")
    if out and out[0]:
        sha, subject = out[0].split(SEP, 1)
        return sha, subject
    return e["sha"], e["subject"]


def history_decisions(repo, events, present_scopes, imported):
    """Libraries that were removed and are still absent => decisions.
    If the code still imports it, it was only made optional: recorded, but nothing is forbidden."""
    last_removal = {}
    for e in events:
        for p in e["removed"]:
            last_removal[(e["scope"], p)] = e
        for p in e["added"]:
            last_removal.pop((e["scope"], p), None)
    out = []
    for (scope, pkg), e in sorted(last_removal.items(), key=lambda kv: kv[1]["ts"]):
        present = set(present_scopes.get(scope, {}))
        if pkg in present:
            continue
        sha, subject = removal_commit(repo, e, pkg)
        named = [m.group(2) or m.group(4) for m in REPLACED.finditer(subject)  # "Replace graphene with Strawberry"
                 if related(m.group(1) or m.group(3), pkg)]
        chose = [a for a in e["added"] if a in present and (related(a, pkg) or any(related(a, n) for n in named))][:2]
        still_imported = any(import_matches(m, pkg) for m in imported)
        if still_imported:
            title, forbid = f"Make {pkg} optional (removed from required dependencies)", []
        else:
            title = f"Replace {pkg} with {', '.join(chose)}" if chose else f"Stop using {pkg}"
            forbid = forbid_tokens(pkg, present)
        why = e["why"].get(pkg, "")
        also = [a for a in e["added"] if a not in chose and a in present]
        rationale = f"{subject}." + (f" It was used for: {why}." if why else "") + \
            (f" Added in the same change: {', '.join(also[:4])}." if also else "")
        out.append(dict(title=title, rationale=rationale, scope=scope, rejected=[pkg], chose=chose, forbid=forbid,
                        sha=sha, ts=e["ts"], author=e["author"], subject=subject))
    return out


# ---------------------------------------------------------------- 5. people

def people(repo, comp_paths, until=None):
    """Person KNOWS Component from git log --name-only (no file contents). Aliases merged by name and email."""
    log = git(repo, "log", "--no-merges", "--format=%x01%an%x00%ae%x00%at", "--name-only")
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    commits = []
    for entry in log.split(REC)[1:]:
        head, _, files = entry.partition("\n")
        name, email, ts = head.split(SEP)
        ts = int(ts) * 1000
        if "[bot]" in name or "[bot]" in email or (until and ts > until):
            continue
        n, e = "n:" + norm(name), "e:" + email.lower()
        parent[find(e)] = find(n)
        commits.append((n, name, email.lower(), ts, [f for f in files.split("\n") if f]))
    knows = defaultdict(lambda: [0, 0])
    persons = defaultdict(lambda: {"names": Counter(), "emails": set()})
    for n, name, email, ts, files in commits:
        key = find(n)
        persons[key]["names"][name] += 1
        persons[key]["emails"].add(email)
        for c in {component_of(f, comp_paths) for f in files}:
            k = knows[(key, c)]
            k[0] += 1
            k[1] = max(k[1], ts)
    rows = [{"key": k, "name": v["names"].most_common(1)[0][0], "emails": sorted(v["emails"])} for k, v in persons.items()]
    return rows, [{"p": p, "c": c, "n": v[0], "last": v[1]} for (p, c), v in knows.items() if v[0] >= 2]


# ---------------------------------------------------------------- 6. notes and rules

def notes(repo, files):
    out = []
    for f in files:
        if Path(f).suffix not in CODE_EXT or "/migrations/" in f:
            continue
        for i, line in enumerate((repo / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = NOTE_MARK.search(line)
            if m and len(m.group(2)) > 15:
                out.append({"id": f"{f}:{i}", "file": f, "line": i, "kind": m.group(1).lower(), "text": m.group(2)[:200]})
    docs = [d for d in DOC_FILES if (repo / d).exists()]
    docs += [f for f in files if f.endswith(".md") and any(f.startswith(a + "/") for a in ADR_DIRS)]
    for d in docs:
        for i, line in enumerate((repo / d).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = line.strip(" -*>")
            if RULE_WORDS.search(s) and 25 < len(s) < 400 and not s.startswith("#"):
                out.append({"id": f"{d}:{i}", "file": d, "line": i, "kind": "rule", "text": s[:300]})
    return out


# ---------------------------------------------------------------- load

def init(repo, name=None, until=None, verbose=True):
    """Scan the repo and (re)build its graph. until = epoch ms: only use history up to then (for replay evals)."""
    t0 = time.time()
    repo = Path(repo).resolve()
    name = name or repo.name
    files = [f for f in git(repo, "ls-files").split("\n") if f]
    groups = components(files)
    comp_paths = sorted(groups)
    deps, uses = scan_imports(repo, files, comp_paths)
    manifests = [f for f in files if f.endswith(PY_MANIFESTS) or f.endswith("package.json")]
    events, present = manifest_history(repo, manifests, until)
    present_names = {p for pkgs in present.values() for p in pkgs}
    unmapped = {m for (_, m) in uses if not any(import_matches(m, l) for l in present_names)}
    decisions = history_decisions(repo, events, present, unmapped)
    persons, knows = people(repo, comp_paths, until)
    found_notes = notes(repo, files)
    first_added = {}
    for e in events:
        for p in e["added"]:
            first_added.setdefault((e["scope"], p), e)

    libs = {}
    for scope, pkgs in present.items():
        for p, why in pkgs.items():
            fa = first_added.get((scope, p), {})
            libs[p] = {"name": p, "why": why, "present": True, "since": fa.get("ts", 0), "added_in": fa.get("subject", "")}
    for d in decisions:
        for p in d["rejected"]:
            libs.setdefault(p, {"name": p, "why": "", "present": False, "since": 0, "added_in": ""})
    import_to_lib = {}
    for (_, mod) in uses:
        match = next((l for l in libs if libs[l]["present"] and norm(l) == norm(mod)), None) or \
            next((l for l in libs if libs[l]["present"] and import_matches(mod, l)), None)
        import_to_lib[mod] = match or mod
        libs.setdefault(import_to_lib[mod], {"name": mod, "why": "", "present": True, "since": 0, "added_in": "unlisted"})

    db.reset()
    db.create_indexes()
    db.write("CREATE (:Repo {name: $n, path: $p, scanned_at: $t})", n=name, p=str(repo), t=int(time.time() * 1000))
    db.bulk("UNWIND $rows AS r CREATE (:Component {name: r.name, path: r.name, files: r.files})",
            [{"name": c, "files": len(ps)} for c, ps in groups.items()])
    db.bulk("UNWIND $rows AS r MATCH (c:Component {name: r.c}) CREATE (:File {path: r.f})-[:IN]->(c)",
            [{"f": f, "c": c} for c, ps in groups.items() for f in ps])
    db.bulk("UNWIND $rows AS r MATCH (a:Component {name: r.a}), (b:Component {name: r.b}) "
            "CREATE (a)-[:DEPENDS_ON {imports: r.n, valid_from: 0}]->(b)",
            [{"a": a, "b": b, "n": n} for (a, b), n in deps.items()])
    db.bulk("UNWIND $rows AS r CREATE (:Library {name: r.name, why: r.why, present: r.present, since: r.since, added_in: r.added_in})",
            list(libs.values()))
    lib_uses = Counter()
    for (src, mod), fs in uses.items():
        lib_uses[(src, import_to_lib[mod])] += len(fs)
    db.bulk("UNWIND $rows AS r MATCH (c:Component {name: r.c}), (l:Library {name: r.l}) CREATE (c)-[:USES {files: r.n}]->(l)",
            [{"c": c, "l": l, "n": n} for (c, l), n in lib_uses.items()])
    db.bulk("UNWIND $rows AS r CREATE (:Person {key: r.key, name: r.name, emails: r.emails})", persons)
    db.bulk("UNWIND $rows AS r MATCH (p:Person {key: r.p}), (c:Component {name: r.c}) "
            "CREATE (p)-[:KNOWS {commits: r.n, last: r.last}]->(c)", knows)
    db.bulk("UNWIND $rows AS r MATCH (f:File {path: r.file}) "
            "CREATE (f)-[:HAS_NOTE]->(:Note {id: r.id, file: r.file, line: r.line, kind: r.kind, text: r.text})",
            [n for n in found_notes if n["kind"] != "rule"])
    db.bulk("UNWIND $rows AS r MATCH (repo:Repo) "
            "CREATE (repo)-[:HAS_NOTE]->(:Note {id: r.id, file: r.file, line: r.line, kind: r.kind, text: r.text})",
            [n for n in found_notes if n["kind"] == "rule"])
    rows = [{**d, "id": f"D-{i}", "about": d["scope"]} for i, d in enumerate(decisions, 1)]
    db.bulk("""UNWIND $rows AS r
        CREATE (d:Decision {id: r.id, title: r.title, rationale: r.rationale, status: 'active', trust: 'historical',
                locked: false, version: 1, forbid: r.forbid, paths: [], valid_from: r.ts, recorded_at: timestamp(),
                actor: 'precedent-scan', source: 'commit:' + substring(r.sha, 0, 10)})
        MERGE (c:Commit {sha: r.sha}) ON CREATE SET c.at = r.ts, c.subject = r.subject, c.author = r.author
        CREATE (d)-[:EVIDENCED_BY]->(c)""", rows)
    db.bulk("UNWIND $rows AS r MATCH (d:Decision {id: r.id}), (repo:Repo) WHERE r.about = '.' CREATE (d)-[:ABOUT]->(repo)", rows)
    db.bulk("UNWIND $rows AS r MATCH (d:Decision {id: r.id}), (c:Component) WHERE r.about <> '.' AND c.name = r.about "
            "CREATE (d)-[:ABOUT]->(c)", rows)
    db.bulk("UNWIND $rows AS r UNWIND r.rejected AS lib MATCH (d:Decision {id: r.id}), (l:Library {name: lib}) "
            "CREATE (d)-[:REJECTED]->(l)", rows)
    db.bulk("UNWIND $rows AS r UNWIND r.chose AS lib MATCH (d:Decision {id: r.id}), (l:Library {name: lib}) "
            "CREATE (d)-[:CHOSE]->(l)", rows)
    db.write("MERGE (c:Counter {name: 'D'}) SET c.n = $n", n=len(rows))
    db.write("MERGE (c:Counter {name: 'C'}) SET c.n = 0")
    db.write("MERGE (c:Counter {name: 'P'}) SET c.n = 0")
    stats = {"repo": name, "files": len(files), "components": len(groups), "depends_on": len(deps),
             "libraries": len(libs), "manifest_events": len(events), "decisions": len(rows),
             "guarded_decisions": sum(bool(d["forbid"]) for d in decisions), "people": len(persons),
             "knows": len(knows), "notes": len(found_notes), "rules": sum(n["kind"] == "rule" for n in found_notes),
             "seconds": round(time.time() - t0, 1), "llm_tokens": 0}
    if verbose:
        print(json.dumps(stats, indent=1))
    return stats
