"""Cold start: build the graph from what every repo already has. No LLM, no docs required.

    python -m precedent init <repo>          (see cli.py)

What it reads, cheapest first:
  1. git ls-files            -> Components (folders) and Files
  2. imports (Python AST, JS/TS import/require) -> Component DEPENDS_ON Component, Component USES Library
  3. manifests at HEAD       -> Library, with the comment written above it ("why")
  4. manifest history        -> Library removed / replaced  => historical Decision (with the commit as evidence)
  5. git log --name-only     -> Person KNOWS Component (aliases merged by name)
  6. agent docs + comments   -> Note (rules like "never write migrations manually"), with file:line
"""

import ast
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

from . import db

CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go"}
JS_EXT = (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs")
# Dependency manifests. Lock files (pip-compile/uv/poetry exports with "# via" or "--hash") are skipped: they list
# transitive packages that come and go without anyone deciding anything.
MANIFEST = re.compile(r"(^|/)(requirements[^/]*\.(txt|in)|requirements/[^/]+\.(txt|in)|pyproject\.toml|Pipfile|setup\.py"
                      r"|setup\.cfg|package\.json)$")
NOT_OURS = re.compile(r"(^|/)(node_modules|vendor|vendored|third_party|fixtures?|examples?|tests?|__tests__|testdata|"
                      r"test_data|samples?)/", re.I)
MANIFEST_GLOBS = [":(glob)**/" + g for g in ("requirements*.txt", "requirements*.in", "requirements/*.txt",
                                              "requirements/*.in", "pyproject.toml", "Pipfile", "setup.py", "setup.cfg",
                                              "package.json")]
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\(\s*)["']([^"'\s]+)["']""")
DOC_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", ".cursorrules")
ADR_DIRS = ("docs/adr", "doc/adr", "docs/decisions", "docs/architecture/decisions", "adr")
RULE_WORDS = re.compile(r"\b(never|do not|don't|must not|must|always|instead of|avoid|not pytest)\b", re.I)
NOTE_MARK = re.compile(r"(?:#|//)\s*(NOTE|IMPORTANT|WARNING|DO NOT|DON'T|NEVER|WHY|DECISION|HACK)\b[:\s-]*(.+)")
REQ_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*)\s*(?:[<>=!~;\[@].*)?$")
REPLACED = re.compile(r"(?:replace|replacing)\s+([\w.-]+)\s+(?:with|by)\s+([\w.-]+)|(?:migrate|move|switch)\s+from\s+([\w.-]+)\s+to\s+([\w.-]+)", re.I)
INTENT = re.compile(r"\b(remov\w*|drop\w*|replac\w*|switch\w*|migrat\w*|get(?:ting)? rid|got rid|no longer|deprecat\w*|"
                    r"unused|clean\s?up|nuke\w*|zap\w*|instead|stop\w* using|move away|rip\w* out|delet\w*|ditch\w*|"
                    r"retir\w*|uninstall\w*|abandon\w*|eliminat\w*|purg\w*)\b", re.I)
PIN = re.compile(r"\b(un)?pin(s|ned|ning)?\b", re.I)
STOP_TOKENS = {"html", "just", "slim", "core", "types", "django", "python", "flask", "swagger", "cookie", "wheel"}
SEP = "\x00"   # field separator in our git log formats (%x00)
REC = "\x01"   # record separator (%x01)


def git(repo, *args):
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout


def read(path):
    """File text, or "" for anything unreadable (submodules, broken symlinks, huge binaries)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.stat().st_size < 2_000_000 else ""
    except OSError:
        return ""


def git_records(repo, *args):
    """Stream `git log` output one commit at a time (each record starts with REC), so a 80k-commit history never
    sits in memory as one string."""
    p = subprocess.Popen(["git", *args], cwd=repo, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    buf = []
    for line in p.stdout:
        if line.startswith(REC) and buf:
            yield "".join(buf)[1:]
            buf = []
        buf.append(line)
    if buf and buf[0].startswith(REC):
        yield "".join(buf)[1:]
    p.wait()


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
    """Longest component path (comp_paths: a set) that is a prefix of this path."""
    parts = path.split("/")
    for i in range(len(parts), 0, -1):
        if "/".join(parts[:i]) in comp_paths:
            return "/".join(parts[:i])
    return "."


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


def js_package(spec):
    """'@sentry/react/x' -> '@sentry/react', 'lodash/get' -> 'lodash'."""
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def scan_imports(repo, files, comp_paths, js_packages=frozenset()):
    """Python: AST imports. JS/TS: import/require strings; relative ones give component dependencies, bare ones count
    as a library only if that name was ever in a package.json (aliases like 'app/x' are not libraries)."""
    tops = python_packages(files)
    stdlib = set(sys.stdlib_module_names)
    deps, uses = Counter(), defaultdict(set)
    for f in files:
        if f.endswith(JS_EXT) and not NOT_OURS.search(f) and not f.endswith(".min.js"):
            src = component_of(f, comp_paths)
            for spec in JS_IMPORT.findall(read(repo / f)):
                if spec.startswith("."):
                    dst = component_of(posixpath.normpath(posixpath.join(posixpath.dirname(f), spec)), comp_paths)
                    if dst != src:
                        deps[(src, dst)] += 1
                elif js_package(spec) in js_packages:
                    uses[(src, js_package(spec))].add(f)
            continue
        if not f.endswith(".py"):
            continue
        src = component_of(f, comp_paths)
        text = read(repo / f)
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


def req_names(items):
    """Package names from PEP 508 strings ('Django>=4.2', 'celery[redis]; python_version > "3.8"')."""
    out = {}
    for s in items:
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9._-]*)", s) if isinstance(s, str) else None  # '3.6.0' is not a package
        if m:
            out[pep503(m.group(1))] = ""
    return out


def parse_toml_deps(text):
    """pyproject.toml (PEP 621, PEP 735 dependency groups, Poetry, uv, PDM) and Pipfile."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:  # a broken historical version: best effort
        return {pep503(n): "" for b in re.findall(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
                for n in re.findall(r"[\"']([A-Za-z][A-Za-z0-9._-]*)", b)}
    project, tool = data.get("project", {}), data.get("tool", {})
    poetry = tool.get("poetry", {})
    lists = [project.get("dependencies", []), *project.get("optional-dependencies", {}).values(),
             *data.get("dependency-groups", {}).values(), tool.get("uv", {}).get("dev-dependencies", []),
             *tool.get("pdm", {}).get("dev-dependencies", {}).values()]
    pkgs = req_names(x for l in lists if isinstance(l, list) for x in l)
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {}),
              *(g.get("dependencies", {}) for g in poetry.get("group", {}).values()),
              data.get("packages", {}), data.get("dev-packages", {})]  # the last two: Pipfile
    pkgs.update({pep503(k): "" for t in tables if isinstance(t, dict) for k in t if k.lower() != "python"})
    return pkgs


def parse_setup(text):
    """setup.py install_requires=[...] or setup.cfg install_requires = <indented lines> (old repos)."""
    block = re.search(r"install_requires\s*=\s*\[(.*?)\]\s*[,)\n]", text, re.S)
    if block:
        return req_names(re.findall(r"[\"']([^\"']+)[\"']", block.group(1)))
    block = re.search(r"install_requires\s*=\s*\n((?:[ \t]+\S.*\n?)+)", text)
    return req_names(block.group(1).split("\n")) if block else {}


def parse_package_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k.lower(): "" for sec in ("dependencies", "devDependencies", "optionalDependencies")
            for k in (data.get(sec) or {}) if not k.startswith("@types/")}  # @types/x follows x, not a decision


def parse_manifest(path, text):
    """{package: why}, or None for a lock-style file (ignored)."""
    if path.endswith("package.json"):
        return parse_package_json(text)
    if path.endswith(("pyproject.toml", "Pipfile")):
        return parse_toml_deps(text)
    if path.endswith(("setup.py", "setup.cfg")):
        return parse_setup(text)
    if "# via" in text or "--hash=" in text:
        return None
    return parse_requirements(text)


def scope_union(state):
    """Python manifests share one scope (the repo, '.'); each package.json is its own folder's scope."""
    scopes = defaultdict(dict)
    for path, pkgs in state.items():
        scope = path.rsplit("/", 1)[0] if path.endswith("package.json") and "/" in path else "."
        for p, why in (pkgs or {}).items():
            scopes[scope][p] = scopes[scope].get(p) or why
    return scopes


def manifest_history(repo, until=None):
    """Walk the mainline (first-parent) history of every manifest that ever existed, oldest first, with the full
    file text per commit. Returns the add/remove events, the package sets at the end (or at `until`) and every
    name ever listed in a package.json (so JS imports of those count as library uses).
    A commit that drops most of a scope's packages at once is a format migration (setup.py -> pyproject, an
    unparsed lock format), not a decision: it is marked `mass` and never becomes one."""
    state, events, js = {}, [], set()
    for entry in git_records(repo, "log", "--first-parent", "--diff-merges=first-parent", "--reverse", "--no-renames",
                             "-p", "-U999999", "--format=%x01%H%x00%at%x00%an%x00%s", "--", *MANIFEST_GLOBS):
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
            path = new.group(1)
            if NOT_OURS.search(path) or not MANIFEST.search(path):
                continue
            lines = [l[1:] for l in diff.split("\n") if l[:1] in (" ", "+") and not l.startswith("+++")]
            state[path] = parse_manifest(path, "\n".join(lines))
            if path.endswith("package.json"):
                js |= set(state[path])
        after = scope_union(state)
        for scope in set(before) | set(after):
            old, now = before.get(scope, {}), after.get(scope, {})
            added, removed = sorted(set(now) - set(old)), sorted(set(old) - set(now))
            if added or removed:
                events.append(dict(sha=sha, ts=ts, author=author, subject=subject, scope=scope, added=added,
                                   removed=removed, why={p: old[p] for p in removed},
                                   mass=len(removed) >= 8 and len(removed) > 0.5 * len(old)))
    return events, scope_union(state), js


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
    """Does Python import name `mod` belong to package `pkg`? (django_filters -> django-filter, rest_framework ->
    djangorestframework, strawberry -> strawberry-graphql; but graphene_django is NOT django)."""
    a, b = norm(mod), norm(pkg)
    return len(a) >= 3 and (a == b or b.startswith(a) or (a.startswith(b) and len(a) - len(b) <= 2)
                            or (len(a) >= 4 and b.endswith(a)))


def related(a, b):
    """Same family of library: psycopg2-binary -> psycopg, react-color -> react-colorful yes; django-cacheops ->
    django-redis no."""
    na, nb = norm(a).removeprefix("django"), norm(b).removeprefix("django")
    if na == nb:
        return True
    if len(na) < 4 or len(nb) < 4:
        return False  # a long shared prefix, not just a family name: @angular/animations is not @angular-builders/jest
    return na in nb or nb in na or len(os.path.commonprefix([na, nb])) >= 0.6 * max(len(na), len(nb))


def removal_commit(repo, e, pkg):
    """For a merge/release commit, find the commit inside the merged branch that touched this package's line."""
    if not git(repo, "rev-parse", "-q", "--verify", e["sha"] + "^2").strip():  # not a merge commit
        return e["sha"], e["subject"]
    pat = re.escape(pkg).replace("\\-", "[-_.]")
    out = git(repo, "log", "--no-merges", "-i", f"-G{pat}", "--format=%H%x00%s", f"{e['sha']}^1..{e['sha']}", "--",
              *MANIFEST_GLOBS).strip().split("\n")
    if out and out[0]:
        sha, subject = out[0].split(SEP, 1)
        return sha, subject
    return e["sha"], e["subject"]


def history_decisions(repo, events, present_scopes, py_imported, js_imported=frozenset()):
    """Libraries that were removed and are not a dependency anywhere in the repo now => decisions (one per library).
    If the code still imports it, it was only made optional: recorded, but nothing is forbidden."""
    last_removal = {}
    for e in events:
        if not e["mass"]:
            for p in e["removed"]:
                last_removal[(e["scope"], p)] = e
        for p in e["added"]:
            last_removal.pop((e["scope"], p), None)
    present = set().union(*present_scopes.values()) if present_scopes else set()
    by_pkg = defaultdict(list)  # the same library dropped from several package.json files -> one decision
    for (scope, pkg), e in last_removal.items():
        if pkg not in present:  # still listed somewhere else = moved, not dropped
            by_pkg[pkg].append((e["ts"], scope, e))
    out = []
    for pkg, removals in sorted(by_pkg.items(), key=lambda kv: max(r[0] for r in kv[1])):
        _, _, e = max(removals, key=lambda r: r[0])
        sha, subject = removal_commit(repo, e, pkg)
        named = [m.group(2) or m.group(4) for m in REPLACED.finditer(subject)  # "Replace graphene with Strawberry"
                 if related(m.group(1) or m.group(3), pkg)]
        chose = [a for a in e["added"] if a in present and (related(a, pkg) or any(related(a, n) for n in named))][:2]
        # confidence: a removal is a decision when the commit names the library, says it removes/replaces something, or
        # adds a replacement. "Bump dependencies" dropping a transitive package is lock churn: kept as 'observed'
        # (shown as context, never blocks or asks).
        confident = bool(chose) or ((mentioned(pkg, subject) or INTENT.search(subject) is not None)
                                    and not PIN.search(subject))  # "remove the pin for X" keeps X
        still_imported = pkg in js_imported or any(import_matches(m, pkg) for m in py_imported)
        if still_imported:
            title, forbid = f"Make {pkg} optional (removed from required dependencies)", []
        else:
            title = f"Replace {pkg} with {', '.join(chose)}" if chose else f"Stop using {pkg}"
            if pkg.replace("-", "_") in sys.stdlib_module_names:
                title += " (the backport: the standard library has it)"
            forbid = forbid_tokens(pkg, present)
        why = e["why"].get(pkg, "")
        also = [a for a in e["added"] if a not in chose and a in present]
        rationale = f"{subject}." + (f" It was used for: {why}." if why else "") + \
            (f" Added in the same change: {', '.join(also[:4])}." if also else "")
        out.append(dict(title=title, rationale=rationale, scopes=sorted({s for _, s, _ in removals}), rejected=[pkg],
                        chose=chose, forbid=forbid, sha=sha, ts=e["ts"], author=e["author"], subject=subject,
                        trust="historical" if confident else "observed"))
    return out


def mentioned(pkg, text):
    """Is the package named in this text ('Drop django-silk', 'Remove graphql_jwt package')?"""
    names = {pkg, pkg.replace("-", "_"), pkg.replace("-", " ")}
    return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I) for n in names if len(n) >= 3)


# ---------------------------------------------------------------- 5. people

def people(repo, comp_paths, until=None):
    """Person KNOWS Component from git log --name-only (no file contents). Aliases merged by name and email."""
    parent, comp_cache = {}, {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    def comp(f):
        if f not in comp_cache:
            comp_cache[f] = component_of(f, comp_paths)
        return comp_cache[f]

    commits = []
    for entry in git_records(repo, "log", "--no-merges", "--format=%x01%an%x00%ae%x00%at", "--name-only"):
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
        for c in {comp(f) for f in files}:
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
        for i, line in enumerate(read(repo / f).splitlines(), 1):
            m = NOTE_MARK.search(line)
            if m and len(m.group(2)) > 15:
                out.append({"id": f"{f}:{i}", "file": f, "line": i, "kind": m.group(1).lower(), "text": m.group(2)[:200]})
    docs = [d for d in DOC_FILES if (repo / d).exists()]
    docs += [f for f in files if f.endswith(".md") and any(f.startswith(a + "/") for a in ADR_DIRS)]
    for d in docs:
        for i, line in enumerate(read(repo / d).splitlines(), 1):
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
    comp_paths = frozenset(groups)
    events, present, js_packages = manifest_history(repo, until)
    deps, uses = scan_imports(repo, files, comp_paths, js_packages)
    # a "removed" package that the code still imports by its own name was only dropped from the manifest
    # (e.g. redis, still imported, pulled in by django-redis). Generic import names (django) never count.
    mods = {m for (_, m) in uses}
    py_imported = {m for m in mods if m not in js_packages and norm(m) not in STOP_TOKENS}
    decisions = history_decisions(repo, events, present, py_imported, mods & js_packages)
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
    present_libs = {norm(l): l for l in libs if libs[l]["present"]}
    import_to_lib = {}
    for mod in mods:
        match = present_libs.get(norm(mod)) or next((l for l in present_libs.values() if import_matches(mod, l)), None)
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
    # where a decision applies: Python manifests (scope '.') -> the whole repo; a package.json -> the components in
    # its folder (or the component that contains the folder)
    rows = [{**d, "id": f"D-{i}", "repo_wide": "." in d["scopes"],
             "comps": sorted({c for s in d["scopes"] if s != "." for c in
                              ([c for c in comp_paths if c == s or c.startswith(s + "/")] or [component_of(s + "/x", comp_paths)])})}
            for i, d in enumerate(decisions, 1)]
    db.bulk("""UNWIND $rows AS r
        CREATE (d:Decision {id: r.id, title: r.title, rationale: r.rationale, status: 'active', trust: r.trust,
                locked: false, version: 1, forbid: r.forbid, paths: [], valid_from: r.ts, recorded_at: timestamp(),
                actor: 'precedent-scan', source: 'commit:' + substring(r.sha, 0, 10)})
        MERGE (c:Commit {sha: r.sha}) ON CREATE SET c.at = r.ts, c.subject = r.subject, c.author = r.author
        CREATE (d)-[:EVIDENCED_BY]->(c)""", rows)
    db.bulk("UNWIND $rows AS r MATCH (d:Decision {id: r.id}), (repo:Repo) WHERE r.repo_wide CREATE (d)-[:ABOUT]->(repo)", rows)
    db.bulk("UNWIND $rows AS r UNWIND r.comps AS cn MATCH (d:Decision {id: r.id}), (c:Component {name: cn}) "
            "CREATE (d)-[:ABOUT]->(c)", rows)
    db.bulk("UNWIND $rows AS r UNWIND r.rejected AS lib MATCH (d:Decision {id: r.id}), (l:Library {name: lib}) "
            "CREATE (d)-[:REJECTED]->(l)", rows)
    db.bulk("UNWIND $rows AS r UNWIND r.chose AS lib MATCH (d:Decision {id: r.id}), (l:Library {name: lib}) "
            "CREATE (d)-[:CHOSE]->(l)", rows)
    db.write("MERGE (c:Counter {name: 'D'}) SET c.n = $n", n=len(rows))
    db.write("MERGE (c:Counter {name: 'C'}) SET c.n = 0")
    db.write("MERGE (c:Counter {name: 'P'}) SET c.n = 0")
    stats = {"repo": name, "files": len(files), "components": len(groups), "depends_on": len(deps),
             "libraries": len(libs), "manifest_events": len(events),
             "format_migrations_skipped": sum(e["mass"] for e in events), "decisions": len(rows),
             "guarded_decisions": sum(bool(d["forbid"]) and d["trust"] == "historical" for d in decisions),
             "observed_only": sum(d["trust"] == "observed" for d in decisions), "people": len(persons),
             "knows": len(knows), "notes": len(found_notes), "rules": sum(n["kind"] == "rule" for n in found_notes),
             "seconds": round(time.time() - t0, 1), "llm_tokens": 0}
    if verbose:
        print(json.dumps(stats, indent=1))
    return stats
