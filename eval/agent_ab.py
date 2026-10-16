"""E6: does a real coding agent undo a past decision, with and without Precedent? (A/B on 6 open-source repos)

Each task asks Claude Code (headless `claude -p`) for a normal change in a real repo. The repo's history says the
library involved was removed or replaced on purpose. Some prompts name the old library (a teammate who doesn't know the
history), some don't (does the agent reach for it by habit?).

  off: the repo as it is, no Precedent (its .mcp.json / .claude/settings.local.json are moved aside)
  on:  `precedent install` (hooks + MCP), graph from `precedent init` (cold start, 0 tokens)

Outcome = the final working tree, not what the agent says: a run "reintroduced" the library if any added line in the
diff (edits, new files, however written) would import it or add it to a manifest. Cost and turns come from claude's JSON.

    python -m eval.agent_ab [task names...]        (budget cap per run: --max-budget-usd 0.25, Haiku)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from precedent import config, db, tools

DATA = Path(__file__).resolve().parent.parent.parent / "_data"
OUT = Path(__file__).parent / "out" / "agent_ab"
MODEL = os.getenv("AB_MODEL", "haiku")
SUFFIX = " Keep the change minimal. Do not run any commands or tests."

# name: (repo, removed library, named in prompt?, prompt)
TASKS = {
    "netbox_cacheops": ("netbox", "django-cacheops", True,
                        "Cache the queryset of the Site list view in netbox/dcim/views.py with django-cacheops "
                        "(`cached_as`), so repeated page loads skip the database."),
    "netbox_drfyasg": ("netbox", "drf-yasg", True,
                       "Document the `trace` action of the Interface API viewset in netbox/dcim/api/views.py with "
                       "drf-yasg's `swagger_auto_schema` decorator (a short description of the response)."),
    "saleor_psycopg2": ("saleor", "psycopg2", True,
                        "Add a Django management command `table_sizes` in saleor/core/management/commands/ that "
                        "connects to Postgres directly with psycopg2 (using settings.DATABASES['default']) and prints "
                        "the 10 largest tables."),
    "saleor_markdown": ("saleor", "markdown", True,
                        "Add a helper `render_markdown(text)` in saleor/core/utils/markdown.py that converts Markdown "
                        "to HTML with the `markdown` package, and add the dependency to pyproject.toml."),
    "saleor_html2text": ("saleor", "html2text", False,
                         "Add a helper `html_to_text(html)` in saleor/core/utils/text.py that turns an HTML email body "
                         "into readable plain text (keep links as 'text (url)')."),
    "paperless_bleach": ("paperless-ngx", "bleach", True,
                         "Add a helper `clean_html(text)` in src/paperless_mail/utils.py that sanitizes HTML from "
                         "emails with bleach, allowing only basic formatting tags."),
    "paperless_tqdm": ("paperless-ngx", "tqdm", False,
                       "Show a progress bar in the `document_thumbnails` management command while it processes "
                       "documents."),
    "wagtail_tablib": ("wagtail", "tablib", True,
                       "Add a function `rows_to_xlsx(headers, rows)` in wagtail/admin/utils.py that returns the bytes "
                       "of an XLSX file, built with tablib."),
    "wagtail_html5lib": ("wagtail", "html5lib", True,
                         "Add a helper `parse_fragment(html)` in wagtail/rich_text/__init__.py that parses an HTML "
                         "fragment with html5lib and returns the parsed tree."),
    "outline_lodash": ("outline", "lodash", False,
                       "On the search page (app/scenes/Search), debounce the search input's change handler by 250 ms "
                       "so we don't fire a request on every keystroke."),
    "outline_koarouter": ("outline", "koa-router", True,
                          "Add a GET /api/healthz route that returns {ok: true}, using koa-router, in a new file "
                          "server/routes/healthz.ts, and mount it next to the other API routes."),
    "mitmproxy_blinker": ("mitmproxy", "blinker", True,
                          "Add a `flow_dropped` signal using blinker in mitmproxy/flow.py, so addons can subscribe "
                          "when a flow is dropped."),
}
INSTALLED = (".mcp.json", ".claude/settings.local.json")


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout


def added_lines(repo):
    """{path: added text} for everything the agent changed or created, however it wrote it."""
    out = {}
    diff = git(repo, "diff", "-U0", "--no-color")
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and path:
            out[path] = out.get(path, "") + line[1:] + "\n"
    for line in git(repo, "status", "--porcelain", "--untracked-files=all").splitlines():
        p = line[3:].strip('"')
        if line.startswith("??") and (repo / p).is_file():
            out[p] = (repo / p).read_text(encoding="utf-8", errors="replace")
    return out


def decision_for(lib):
    rows = db.read("""MATCH (d:Decision)-[:REJECTED]->(:Library {name: $l}) OPTIONAL MATCH (d)-[:EVIDENCED_BY]->(c:Commit)
        RETURN d.id, d.title, d.forbid, d.trust, c.sha, c.subject""", l=lib)
    return dict(zip(["id", "title", "forbid", "trust", "sha", "subject"], rows[0])) if rows else None


def run(name, arm):
    repo_name, lib, named, prompt = TASKS[name]
    repo = DATA / repo_name
    config.GRAPH, config.REPO = f"cs_{repo_name}", str(repo)
    db.graph.cache_clear()
    d = decision_for(lib)
    aside = repo / ".precedent_off"
    if arm == "off":
        aside.mkdir(exist_ok=True)
        for f in INSTALLED:
            if (repo / f).exists():
                shutil.move(str(repo / f), str(aside / Path(f).name))
    t0 = time.time()
    try:
        p = subprocess.run([shutil.which("claude"), "-p", prompt + SUFFIX, "--model", MODEL, "--output-format", "json",
                            "--max-turns", "20", "--max-budget-usd", "0.25", "--permission-mode", "acceptEdits"],
                           cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=900)
        out = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"result": p.stdout[-1500:], "stderr": p.stderr[-1500:]}
    except subprocess.TimeoutExpired:
        out = {"result": "timeout"}
    finally:
        if arm == "off":
            for f in INSTALLED:
                if (aside / Path(f).name).exists():
                    shutil.move(str(aside / Path(f).name), str(repo / f))
    changed = added_lines(repo)
    hits = [(path, next((l for l in text.splitlines() if tools.violates(tok, path, l)), "").strip()[:120])
            for path, text in changed.items() for tok in (d["forbid"] if d else [])
            if tools.violates(tok, path, text)]
    result = out.get("result") or ""
    low = result.lower()
    u = out.get("usage", {})
    report = {
        "task": name, "arm": arm, "repo": repo_name, "library": lib, "named_in_prompt": named, "model": MODEL,
        "decision": d, "prompt": prompt + SUFFIX,
        "reintroduced": bool(hits), "evidence": hits[:5],
        "files_changed": sorted(changed),
        "mentions_decision": bool(d) and (d["id"].lower() in low or (d["sha"] or "zz")[:7] in low
                                          or any(w in low for w in ("removed", "replaced", "no longer", "decision",
                                                                    "precedent", "deprecated", "dropped"))),
        "turns": out.get("num_turns"), "cost_usd": out.get("total_cost_usd"), "seconds": round(time.time() - t0, 1),
        "tokens_in": sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        "tokens_out": u.get("output_tokens"), "permission_denials": out.get("permission_denials"),
        "result": result[:3000],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}__{arm}.json").write_text(json.dumps(report, indent=1, default=str))
    git(repo, "checkout", "--", ".")
    git(repo, "clean", "-fdq")  # installed files are in info/exclude, so they stay
    return report


def main():
    names = [a for a in sys.argv[1:] if a in TASKS] or list(TASKS)
    spent = 0.0
    for name in names:
        for arm in ("off", "on"):
            if (OUT / f"{name}__{arm}.json").exists():
                continue
            r = run(name, arm)
            spent += r["cost_usd"] or 0
            print(f"{name:20s} {arm:3s} reintroduced={r['reintroduced']!s:5s} mentions={r['mentions_decision']!s:5s} "
                  f"turns={r['turns']} ${r['cost_usd']} spent=${spent:.2f} files={r['files_changed']}", flush=True)
            if spent > float(os.getenv("AB_BUDGET", "3.0")):
                print("budget reached, stopping")
                return


if __name__ == "__main__":
    main()
