"""The public demo: one web server for the graph view, the chat panel and a read-only MCP endpoint.

    uvicorn precedent.web:app --port 8000        (PRECEDENT_GRAPH=mitmproxy, OPENAI_API_KEY for the chat panel)

/mcp         the real MCP server over Streamable HTTP (stateless), read tools only. Add it to any agent:
             claude mcp add --transport http precedent https://<host>/mcp
/api/graph   the graph for the browser (components, decisions, libraries, top people; no emails)
/api/chat    a small agent: the model only gets the tools listed by our own /mcp, and calls them through it
/            the page (precedent/static)
"""

import json
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx2 as httpx  # the HTTP client the mcp SDK already depends on
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from . import config, db, mcp_server as m

STATIC = Path(__file__).parent / "static"
QUERIES = Path(__file__).parent / "queries"
REPO_URL = os.getenv("PRECEDENT_REPO_URL", "https://github.com/mitmproxy/mitmproxy")
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini-2025-08-07")
PORT = int(os.getenv("PORT", "8000"))
MAX_TURNS = 6
PER_IP_PER_HOUR = int(os.getenv("CHAT_PER_IP_PER_HOUR", "20"))

public = MCPServer("precedent", instructions=(
    f"Precedent: the decision graph of {REPO_URL} on FalkorDB, built from its code and git history. "
    "Read-only here. Use why/search/impact/who_knows/history/check/context, or query for read-only Cypher."))
for tool in (m.context, m.check, m.why, m.impact, m.who_knows, m.history, m.search, m.query):
    public.tool()(tool)


@public.custom_route("/api/graph", methods=["GET"])
async def graph(request: Request):
    nodes, edges = {}, []

    def node(i, kind, label, **props):
        nodes.setdefault(i, {"id": i, "kind": kind, "label": label, "props": props})

    repo = db.read("MATCH (r:Repo) RETURN r.name LIMIT 1")
    node("repo", "repo", (repo[0][0] if repo else "repo"))
    for name, files in db.read("MATCH (c:Component) OPTIONAL MATCH (f:File)-[:IN]->(c) RETURN c.name, count(f)"):
        node("c:" + name, "component", name, files=files)
    for a, b, n in db.read("MATCH (a:Component)-[r:DEPENDS_ON]->(b:Component) RETURN a.name, b.name, r.imports"):
        edges.append(["c:" + a, "c:" + b, "DEPENDS_ON"])
    for c, lib, n in db.read("MATCH (c:Component)-[r:USES]->(l:Library) RETURN c.name, l.name, r.files"):
        node("l:" + lib, "library", lib)
        edges.append(["c:" + c, "l:" + lib, "USES"])
    rows = db.read("""MATCH (d:Decision) OPTIONAL MATCH (d)-[:EVIDENCED_BY]->(k:Commit)
        RETURN d.id, d.title, d.rationale, d.status, d.trust, d.valid_from, d.source, k.sha, k.subject, k.author,
               [(d)-[:ABOUT]->(x:Component) | x.name], [(d)-[:ABOUT]->(y:Repo) | 1],
               [(d)-[:CHOSE]->(l1:Library) | l1.name], [(d)-[:REJECTED]->(l2:Library) | l2.name]""")
    for i, title, why, status, trust, since, source, sha, subject, author, about, repo_wide, chose, rejected in rows:
        node(i, "decision", i, title=title, rationale=why, status=status, trust=trust, since=since, source=source,
             commit=sha, commit_url=f"{REPO_URL}/commit/{sha}" if sha else None, subject=subject, author=author,
             chose=chose, rejected=rejected)
        for x in about:
            edges.append([i, "c:" + x, "ABOUT"])
        if repo_wide:
            edges.append([i, "repo", "ABOUT"])
        for lib in chose:
            node("l:" + lib, "library", lib)
            edges.append([i, "l:" + lib, "CHOSE"])
        for lib in rejected:
            node("l:" + lib, "library", lib)
            edges.append([i, "l:" + lib, "REJECTED"])
    for c, people in db.read("""MATCH (p:Person)-[k:KNOWS]->(c:Component) WITH c, p, k ORDER BY k.commits DESC
            RETURN c.name, collect([p.key, p.name, k.commits])[0..3]"""):
        for key, name, commits in people:
            node("p:" + key, "person", name)
            edges.append(["p:" + key, "c:" + c, "KNOWS"])
    edges = [e for e in edges if e[0] in nodes and e[1] in nodes]
    return JSONResponse({"repo": REPO_URL, "nodes": list(nodes.values()), "edges": edges})


# ---------------------------------------------------------------- chat: the model sees only our MCP tools

SYSTEM = (f"You answer questions about the {REPO_URL} codebase using Precedent, its decision graph in FalkorDB. "
          "Use the tools for every fact and never answer from memory. "
          "Which tool: a library name (reactify, mock) -> why(library). A decision id (D-32) -> why(id), which gives the "
          "reason, date and source commit. 'Can I add/use X in file Y?' -> check(path, new_text) with the code line, e.g. "
          "check('test/mitmproxy/test_x.py', 'import mock'). 'What breaks if I change C?' -> impact(C). "
          "'Who knows C?' -> who_knows(C). A topic -> search(a few keywords). Components are folders like 'web', "
          "'mitmproxy', 'test/mitmproxy/proxy'. Never pass as_of unless the user names a date. "
          "query(cypher) is read-only Cypher. Schema: (:Decision {id,title,rationale,status,trust,valid_from,source})"
          "-[:ABOUT]->(:Component {name}) or (:Repo); (:Decision)-[:CHOSE|REJECTED]->(:Library {name}); "
          "(:Decision)-[:EVIDENCED_BY]->(:Commit {sha,at,subject,author}); (:File {path})-[:IN]->(:Component); "
          "(:Component)-[:DEPENDS_ON]->(:Component); (:Component)-[:USES]->(:Library); "
          "(:Person {name})-[:KNOWS {commits,last}]->(:Component). Times are epoch ms. "
          "Give one complete, short answer in markdown: the facts, decision ids, commit and date. "
          "Do not ask the user follow-up questions or offer to fetch more.")


def cypher_for(name, args):
    """The Cypher behind a tool call, shown in the UI next to the result."""
    files = {"impact": ["impact"], "who_knows": ["who_knows"], "history": ["history"], "search": ["search"],
             "check": ["component_of_file", "governing"], "context": ["component_of_file", "governing"]}.get(name)
    if name == "query":
        return args.get("cypher", "")
    if name == "why":
        files = ["as_of"] if args.get("as_of") else ["lineage"] if re.fullmatch(r"[DP]-\d+", args.get("target", "")) else ["search"]
    return "\n\n".join((QUERIES / f"{f}.cypher").read_text(encoding="utf-8").strip() for f in files or [])


hits = defaultdict(deque)


def allowed(ip):
    q, now = hits[ip], time.time()
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= PER_IP_PER_HOUR:
        return False
    q.append(now)
    return True


@public.custom_route("/api/chat", methods=["POST"])
async def chat(request: Request):
    body = await request.json()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    key = os.getenv("OPENAI_API_KEY")
    history = [{"role": r["role"], "content": str(r["content"])[:2000]} for r in body.get("messages", [])[-8:]
               if r.get("role") in ("user", "assistant")]

    async def run():
        def out(**ev):
            return json.dumps(ev) + "\n"
        if not key:
            yield out(t="error", text="The chat panel is not configured on this server (no OPENAI_API_KEY).")
            return
        if not allowed(ip):
            yield out(t="error", text=f"Limit reached ({PER_IP_PER_HOUR} questions per hour). Try again later.")
            return
        async with Client(f"http://127.0.0.1:{PORT}/mcp") as mcp, httpx.AsyncClient(timeout=90) as http:
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                       "parameters": t.input_schema}}
                     for t in (await mcp.list_tools()).tools]
            msgs = [{"role": "system", "content": SYSTEM}] + history
            for _ in range(MAX_TURNS):
                r = await http.post("https://api.openai.com/v1/chat/completions",
                                    headers={"Authorization": f"Bearer {key}"},
                                    json={"model": MODEL, "messages": msgs, "tools": tools, "reasoning_effort": "low"})
                if r.status_code != 200:
                    yield out(t="error", text=f"Model error {r.status_code}: {r.text[:300]}")
                    return
                msg = r.json()["choices"][0]["message"]
                msgs.append(msg)
                if not msg.get("tool_calls"):
                    yield out(t="answer", text=msg.get("content") or "")
                    return
                for call in msg["tool_calls"]:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    yield out(t="call", name=name, args=args, cypher=cypher_for(name, args))
                    res = await mcp.call_tool(name, args)
                    text = "\n".join(getattr(c, "text", "") for c in res.content)[:6000]
                    yield out(t="result", name=name, text=text)
                    msgs.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            yield out(t="answer", text="(stopped after 6 tool rounds)")

    return StreamingResponse(run(), media_type="application/x-ndjson")


@public.custom_route("/", methods=["GET"])
async def index(request: Request):
    return FileResponse(STATIC / "index.html")


app = public.streamable_http_app(
    stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))  # public, read-only
app.mount("/static", StaticFiles(directory=STATIC), name="static")
