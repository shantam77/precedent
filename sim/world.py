"""Synthetic monorepo, straight into the graph (no git needed): volume for the scale and concurrency experiments.

    python -m sim.world --components 400 --files-per 50 --decisions 5000 --people 300 --graph synthetic

Everything is seeded and deterministic. The shape mirrors a real company: services and shared libs, a dependency
DAG, teams owning components, library choices with rejected alternatives, supersession chains (decisions that
changed over time), a trust mix (scan/historical, docs, humans, agents), a few locked decisions and path rules.
"""

import argparse
import random
import time

from precedent import config, db

DOMAINS = ["billing", "auth", "search", "cart", "checkout", "catalog", "notifications", "payments", "inventory",
           "shipping", "users", "gateway", "analytics", "reports", "media", "email", "sms", "pricing", "reviews",
           "recommendations", "fraud", "ledger", "tax", "orders", "returns", "loyalty", "ads", "cms", "i18n", "flags"]
LIBS = {  # category -> alternatives (real library names)
    "cache": ["redis", "pymemcache", "keydb"], "queue": ["celery", "rq", "dramatiq", "kafka-python"],
    "http": ["httpx", "requests", "aiohttp"], "orm": ["sqlalchemy", "peewee", "tortoise-orm"],
    "json": ["orjson", "ujson", "msgspec"], "validation": ["pydantic", "marshmallow", "attrs"],
    "tests": ["pytest", "nose2"], "logging": ["structlog", "loguru"], "retry": ["tenacity", "backoff"],
    "dates": ["pendulum", "arrow"], "templating": ["jinja2", "mako"], "metrics": ["prometheus-client", "statsd"],
    "search": ["opensearch-py", "elasticsearch", "meilisearch"], "pdf": ["weasyprint", "reportlab"],
    "crypto": ["cryptography", "pycryptodome"], "graphql": ["strawberry-graphql", "graphene"],
}
RULES = [("Store money as integer cents", "float(", "Floats lost cents in invoices (INC-212)."),
         ("No raw SQL in request handlers", "cursor.execute(", "SQL injection review found two cases in 2025."),
         ("Never log request bodies", "log.info(request.body", "Bodies contain card data (PCI)."),
         ("Use the shared HTTP client", "requests.get(", "Retries, timeouts and tracing live in libs/http."),
         ("No print() in services", "print(", "stdout is not collected in production.")]
DAY = 86400 * 1000


def build(components=400, files_per=50, decisions=5000, people=300, teams=30, sessions=3000, seed=7, graph="synthetic"):
    rnd = random.Random(seed)
    config.GRAPH = graph
    t0 = time.time()
    db.reset()
    db.create_indexes()
    now = int(time.time() * 1000)
    start = now - 3 * 365 * DAY
    comps = []
    for i in range(components):
        kind = "libs" if i % 5 == 0 else "services"
        comps.append(f"{kind}/{DOMAINS[i % len(DOMAINS)]}{'' if i < len(DOMAINS) else '-' + str(i // len(DOMAINS))}")
    db.write("CREATE (:Repo {name: $n, path: '/synthetic', scanned_at: $t})", n=graph, t=now)
    db.bulk("UNWIND $rows AS r CREATE (:Component {name: r, path: r, files: $k})".replace("$k", str(files_per)), comps)
    files = [{"f": f"{c}/{m}_{j}.py", "c": c} for c in comps for j, m in
             ((j, rnd.choice(["models", "views", "api", "tasks", "utils", "client", "schema", "service"])) for j in range(files_per))]
    db.bulk("UNWIND $rows AS r MATCH (c:Component {name: r.c}) CREATE (:File {path: r.f})-[:IN]->(c)", files, size=2000)
    deps = set()
    for i, c in enumerate(comps):
        libs_before = [x for x in comps[:i] if x.startswith("libs/")]
        for d in rnd.sample(libs_before, min(len(libs_before), rnd.randint(1, 3))) + \
                rnd.sample(comps[:i], min(i, rnd.randint(0, 3))):
            if d != c:
                deps.add((c, d))
    db.bulk("UNWIND $rows AS r MATCH (a:Component {name: r.a}), (b:Component {name: r.b}) "
            "CREATE (a)-[:DEPENDS_ON {imports: r.n, valid_from: 0}]->(b)",
            [{"a": a, "b": b, "n": rnd.randint(1, 40)} for a, b in deps])
    all_libs = sorted({l for alts in LIBS.values() for l in alts})
    db.bulk("UNWIND $rows AS r CREATE (:Library {name: r, why: '', present: true, since: 0, added_in: 'synthetic'})", all_libs)
    persons = [{"key": f"e:dev{i}@corp.example", "name": f"Dev {i}", "emails": [f"dev{i}@corp.example"]} for i in range(people)]
    db.bulk("UNWIND $rows AS r CREATE (:Person {key: r.key, name: r.name, emails: r.emails})", persons)
    db.bulk("UNWIND $rows AS r CREATE (:Team {name: r})", [f"team-{i}" for i in range(teams)])
    db.bulk("UNWIND $rows AS r MATCH (p:Person {key: r.p}), (t:Team {name: r.t}) CREATE (p)-[:MEMBER_OF]->(t)",
            [{"p": p["key"], "t": f"team-{i % teams}"} for i, p in enumerate(persons)])
    db.bulk("UNWIND $rows AS r MATCH (t:Team {name: r.t}), (c:Component {name: r.c}) CREATE (t)-[:OWNS]->(c)",
            [{"t": f"team-{i % teams}", "c": c} for i, c in enumerate(comps)])
    db.bulk("UNWIND $rows AS r MATCH (p:Person {key: r.p}), (c:Component {name: r.c}) CREATE (p)-[:KNOWS {commits: r.n, last: r.l}]->(c)",
            [{"p": persons[(i * 7 + k) % people]["key"], "c": c, "n": rnd.randint(2, 300), "l": now - rnd.randint(0, 700) * DAY}
             for i, c in enumerate(comps) for k in range(5)], size=2000)

    # decisions: per (component, category) a chain of 1..3 versions; plus path rules; trust mix
    rows, sup, n = [], [], 0
    trust_mix = ["historical"] * 4 + ["documented"] * 2 + ["human"] * 2 + ["agent"] * 2
    cats = list(LIBS)
    while n < decisions:
        c = rnd.choice(comps)
        if rnd.random() < 0.08:
            title, token, why = rnd.choice(RULES)
            n += 1
            rows.append(dict(id=f"D-{n}", title=f"{title} in {c}", rationale=why, status="active", trust=rnd.choice(trust_mix),
                             locked=rnd.random() < 0.05, forbid=[token], paths=[f"{c}/*"], about=c, chose=[], rejected=[],
                             vf=start + rnd.randint(0, 900) * DAY, vt=None, actor=rnd.choice(persons)["emails"][0]))
            continue
        cat = rnd.choice(cats)
        alts = LIBS[cat][:]
        rnd.shuffle(alts)
        versions = min(len(alts), rnd.choice([1, 1, 1, 2, 2, 3]))
        t = start + rnd.randint(0, 400) * DAY
        prev = None
        for v in range(versions):
            n += 1
            chose, rejected = alts[v], [a for a in alts if a != alts[v]]
            last = v == versions - 1
            t_end = min(t + rnd.randint(60, 400) * DAY, now - (versions - v) * DAY)  # history stays in the past
            rows.append(dict(id=f"D-{n}", title=f"Use {chose} for {cat} in {c}",
                             rationale=f"{chose} fits {c}: {rnd.choice(['faster', 'simpler to run', 'team knows it', 'fewer deps', 'better async support'])}; "
                                       f"rejected {', '.join(rejected)}.",
                             status="active" if last else "superseded", trust=rnd.choice(trust_mix), locked=rnd.random() < 0.05,
                             forbid=[f"lib:{r}" for r in rejected], paths=[], about=c, chose=[chose], rejected=rejected,
                             vf=t, vt=None if last else t_end, actor=rnd.choice(persons)["emails"][0]))
            if prev:
                sup.append({"new": f"D-{n}", "old": prev})
            prev, t = f"D-{n}", t_end
    db.bulk("""UNWIND $rows AS r CREATE (d:Decision {id: r.id, title: r.title, rationale: r.rationale, status: r.status,
        trust: r.trust, locked: r.locked, version: 1, forbid: r.forbid, paths: r.paths, valid_from: r.vf,
        recorded_at: r.vf, actor: r.actor, source: 'synthetic'})
        WITH d, r WHERE r.vt IS NOT NULL SET d.valid_to = r.vt""", rows, size=1000)
    db.bulk("UNWIND $rows AS r MATCH (d:Decision {id: r.id}), (c:Component {name: r.about}) CREATE (d)-[:ABOUT]->(c)", rows, size=1000)
    db.bulk("UNWIND $rows AS r UNWIND r.chose AS l MATCH (d:Decision {id: r.id}), (x:Library {name: l}) CREATE (d)-[:CHOSE]->(x)", rows, size=1000)
    db.bulk("UNWIND $rows AS r UNWIND r.rejected AS l MATCH (d:Decision {id: r.id}), (x:Library {name: l}) CREATE (d)-[:REJECTED]->(x)", rows, size=1000)
    db.bulk("UNWIND $rows AS r MATCH (n:Decision {id: r.new}), (o:Decision {id: r.old}) CREATE (n)-[:SUPERSEDES {at: 0}]->(o)", sup)
    # past sessions: who edited what (agents and people), for history and "since your last session"
    srows = [{"id": f"past-{i}", "p": persons[i % people]["key"], "h": rnd.choice(["claude-code", "cursor", "cli"]),
              "t": now - rnd.randint(0, 300) * DAY, "files": [rnd.choice(files)["f"] for _ in range(5)]} for i in range(sessions)]
    db.bulk("""UNWIND $rows AS r MATCH (p:Person {key: r.p}) CREATE (p)-[:RAN]->(s:Session {id: r.id, harness: r.h,
        started_at: r.t, writes: 0, actor: p.emails[0], kind: 'agent'}) WITH s, r UNWIND r.files AS f
        MATCH (x:File {path: f}) MERGE (s)-[:EDITED]->(x)""", srows, size=500)
    db.write("MERGE (c:Counter {name: 'D'}) SET c.n = $n", n=n)
    db.write("MERGE (c:Counter {name: 'C'}) SET c.n = 0")
    counts = {l: db.read(f"MATCH (x:{l}) RETURN count(x)")[0][0] for l in
              ["Component", "File", "Library", "Person", "Team", "Decision", "Session"]}
    counts["edges"] = db.read("MATCH ()-[r]->() RETURN count(r)")[0][0]
    counts["seconds"] = round(time.time() - t0, 1)
    print(counts)
    return {"comps": comps, "files": [f["f"] for f in files], "persons": persons, "rules": RULES, "libs": LIBS}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--components", type=int, default=400)
    ap.add_argument("--files-per", type=int, default=50)
    ap.add_argument("--decisions", type=int, default=5000)
    ap.add_argument("--people", type=int, default=300)
    ap.add_argument("--graph", default="synthetic")
    a = ap.parse_args()
    build(a.components, a.files_per, a.decisions, a.people, graph=a.graph)
