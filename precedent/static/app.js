// Graph view (vis-network) + agent panel. Everything shown comes from /api/graph and /api/chat.
const COLORS = { component: "#2dd4bf", decision: "#f5b942", library: "#a78bfa", person: "#7aa2f7", repo: "#f1f5f9" };
const EDGE = { CHOSE: "#4ade80", REJECTED: "#f87171", DEPENDS_ON: "#2dd4bf", ABOUT: "#6b5a2e", USES: "#3b3355", KNOWS: "#2a3a5c" };
const SIZE = { repo: 30, component: 20, decision: 9, library: 7, person: 7 };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const md = (s) => marked.parse(esc(s));
let data, nodes, edges, net, byId = {}, chat = [];

$("mcpurl").textContent = location.origin + "/mcp";
$("copy").onclick = () => navigator.clipboard.writeText(`claude mcp add --transport http precedent ${location.origin}/mcp`);
$("legend").innerHTML = Object.entries(COLORS).map(([k, c]) => `<span><i style="background:${c}"></i>${k}</span>`).join("")
  + `<span><i style="background:${EDGE.CHOSE}"></i>chose</span><span><i style="background:${EDGE.REJECTED}"></i>rejected</span>`;

async function load() {
  data = await (await fetch("/api/graph")).json();
  $("repo").textContent = data.repo.replace("https://github.com/", "");
  $("repo").href = data.repo;
  data.nodes.forEach((n) => (byId[n.id] = n));
  nodes = new vis.DataSet(data.nodes.map((n) => ({
    id: n.id, label: n.kind === "decision" ? `${n.id}` : n.label, title: n.props.title || n.label, kind: n.kind,
    color: { background: COLORS[n.kind], border: COLORS[n.kind], highlight: { background: "#fff", border: COLORS[n.kind] } },
    size: n.kind === "component" ? 12 + Math.min(18, Math.sqrt(n.props.files || 1)) : SIZE[n.kind],
    font: { color: "#cbd5e1", size: n.kind === "component" || n.kind === "repo" ? 16 : 11, strokeWidth: 3, strokeColor: "#0e1116" },
    shape: n.kind === "decision" ? "diamond" : "dot",
  })));
  edges = new vis.DataSet(data.edges.map(([a, b, t], i) => ({
    id: i, from: a, to: b, label: t === "CHOSE" || t === "REJECTED" ? t.toLowerCase() : undefined, rel: t,
    color: { color: EDGE[t] || "#333", highlight: "#fff" }, width: t === "REJECTED" || t === "CHOSE" ? 1.6 : 0.8,
    arrows: { to: { enabled: true, scaleFactor: 0.4 } }, font: { size: 9, color: "#8b95a3", strokeWidth: 0 },
  })));
  net = new vis.Network($("graph"), { nodes, edges }, {
    physics: { solver: "forceAtlas2Based", forceAtlas2Based: { gravitationalConstant: -60, springLength: 90, avoidOverlap: 0.4 },
      stabilization: { iterations: 400 } },
    interaction: { hover: true, tooltipDelay: 150, hideEdgesOnDrag: true },
  });
  net.once("stabilizationIterationsDone", () => { net.setOptions({ physics: false }); $("stats").textContent = summary; });
  net.on("click", (e) => (e.nodes.length ? show(e.nodes[0]) : ($("detail").hidden = true)));
  const count = (k) => data.nodes.filter((n) => n.kind === k).length;
  $("stats").textContent = "laying out the graph…";
  const summary = `${count("component")} components · ${count("decision")} decisions · ${count("library")} libraries · ${count("person")} people · ${data.edges.length} relationships · live from FalkorDB`;
}

function show(id) {
  const n = byId[id]; if (!n) return;
  const p = n.props, row = (k, v) => (v || v === 0) && (!Array.isArray(v) || v.length) ? `<div><span class="k">${k}:</span> ${v}</div>` : "";
  const links = edges.get({ filter: (e) => e.from === id || e.to === id })
    .map((e) => `${e.from === id ? "→" : "←"} ${e.rel} ${esc(byId[e.from === id ? e.to : e.from]?.label)}`).slice(0, 12);
  $("detail").innerHTML = `<button class="x" onclick="this.parentNode.hidden=true">×</button>
    <h3>${esc(n.kind === "decision" ? `${n.id} · ${p.title}` : n.label)}</h3><div class="k">${n.kind}</div>
    ${row("why", esc(p.rationale))}${row("status", esc(p.status))}${row("trust", esc(p.trust))}
    ${row("since", p.since ? new Date(p.since).toISOString().slice(0, 10) : "")}
    ${row("chose", esc((p.chose || []).join(", ")))}${row("rejected", esc((p.rejected || []).join(", ")))}
    ${row("commit", p.commit ? `<a href="${esc(p.commit_url)}" target="_blank" rel="noopener">${esc(p.commit.slice(0, 10))}</a> ${esc(p.subject)} (${esc(p.author)})` : "")}
    ${row("files", p.files)}
    <div style="margin-top:8px" class="k">${links.join("<br>")}</div>`;
  $("detail").hidden = false;
}

function focus(ids) {
  ids = [...new Set(ids)].filter((i) => byId[i]);
  if (!ids.length) return;
  net.selectNodes(ids);
  const view = ids.length > 3 ? ids : [...new Set(ids.flatMap((i) => [i, ...net.getConnectedNodes(i)]))];  // show the neighbourhood too
  net.fit({ nodes: view, animation: { duration: 700 }, maxZoomLevel: 1.2 });
  if (ids.length === 1) show(ids[0]);
}

function idsIn(text, args) {
  const ids = (text.match(/\bD-\d+\b/g) || []).filter((i) => byId[i]);
  for (const v of Object.values(args || {})) {
    if (typeof v !== "string") continue;
    for (const k of ["c:", "l:"]) if (byId[k + v]) ids.push(k + v);
    if (/^D-\d+$/.test(v)) ids.push(v);
  }
  return ids;
}

$("find").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const q = e.target.value.trim().toLowerCase(); if (!q) return;
  const hit = data.nodes.filter((n) => n.id.toLowerCase() === q || n.label.toLowerCase() === q);
  focus((hit.length ? hit : data.nodes.filter((n) => n.label.toLowerCase().includes(q) || (n.props.title || "").toLowerCase().includes(q))).map((n) => n.id).slice(0, 30));
});

const CHIPS = [
  "Why did mitmproxy stop using reactify?",
  "What breaks if I change the mitmproxy component?",
  "Who knows the web folder best?",
  "Which libraries did the team remove from the web UI?",
  "Can I add `import mock` to a test file?",
];
$("chips").innerHTML = CHIPS.map((c) => `<button>${esc(c)}</button>`).join("");
$("chips").onclick = (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); };
$("ask").onsubmit = (e) => { e.preventDefault(); const q = $("q").value.trim(); if (q) { $("q").value = ""; ask(q); } };

function add(html, cls) { const d = document.createElement("div"); d.className = cls || ""; d.innerHTML = html; $("log").appendChild(d); $("log").scrollTop = 1e9; return d; }

async function ask(q) {
  document.querySelector(".intro")?.remove();
  add(esc(q), "msg user");
  chat.push({ role: "user", content: q });
  const wait = add("thinking…", "spin");
  let calls = {}, seen = [];
  try {
    const r = await fetch("/api/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ messages: chat }) });
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1);
        if (ev.t === "call") {
          const el = document.createElement("details"); el.className = "call";
          el.innerHTML = `<summary>MCP tool <code>${esc(ev.name)}(${esc(JSON.stringify(ev.args))})</code></summary>
            ${ev.cypher ? `<div class="lab">Cypher</div><pre>${esc(ev.cypher)}</pre>` : ""}<div class="lab">Result</div><pre class="res">…</pre>`;
          wait.before(el); calls[ev.name] = { el, args: ev.args };
        } else if (ev.t === "result") {
          const c = calls[ev.name]; if (c) c.el.querySelector(".res").textContent = ev.text || "(empty)";
          seen.push(...idsIn(ev.text, c?.args)); focus(seen);
        } else if (ev.t === "answer") {
          add(md(ev.text), "msg answer"); chat.push({ role: "assistant", content: ev.text });
          const own = idsIn(ev.text); if (own.length) focus(own);
        } else if (ev.t === "error") add(esc(ev.text), "msg error");
      }
    }
  } catch (e) { add(esc("Request failed: " + e), "msg error"); }
  wait.remove();
}

load();
