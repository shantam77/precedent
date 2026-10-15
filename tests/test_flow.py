"""End to end: cold start, the guard, agent writes, conflicts, races, proposals, approval and revert."""

from precedent import config, db, store, tools


def agent(session="s-agent-1", email="bot@example.com"):
    return store.Actor(email, "agent", "claude-code", session)


def human(email="asha@example.com"):
    return store.Actor(email, "human", "cli", "s-human")


def test_cold_start_finds_the_replacement(graph):
    assert graph["components"] >= 2 and graph["llm_tokens"] == 0
    rows = db.read("MATCH (d:Decision)-[:CHOSE]->(l:Library) RETURN d.id, d.title, l.name")
    assert any("pymemcache" in t and lib == "redis" for _, t, lib in rows), rows
    assert db.read("MATCH (:Person)-[k:KNOWS]->(:Component) RETURN count(k)")[0][0] > 0
    assert db.read("MATCH (:Repo)-[:HAS_NOTE]->(n:Note {kind: 'rule'}) RETURN count(n)")[0][0] == 1


def test_guard_asks_on_reintroducing_a_replaced_library(graph):
    r = tools.check("src/cache/client.py", "import pymemcache\n", "import redis\n")
    assert r["verdict"] == "ask" and "pub/sub" in r["text"]
    assert tools.check("src/cache/client.py", "import redis\nimport json\n", "import redis\n")["verdict"] == "ok"
    assert tools.check("requirements.txt", "redis\npymemcache==4\n", "redis\n")["verdict"] == "ask"


def test_context_is_short_and_relevant(graph):
    text, ids = tools.context(["src/cache/client.py"])
    assert ids and "redis" in text and len(text) < 600
    billing = tools.context(["src/billing/invoice.py"])[0]
    assert "D-" not in billing and "integer cents" in billing  # no library talk there, only the code note


def test_agent_records_then_duplicate_and_conflict_are_refused(graph):
    a = agent()
    r = store.record_decision(a, "Store money as integer cents", "Floats lose cents in invoices; we had rounding bugs",
                              paths=["src/billing/*"], forbid=["float("], evidence=["src/billing/invoice.py:2"])
    assert r["status"] == "applied", r
    dup = store.record_decision(a, "Money stored as integer cents", "same thing again, said differently",
                                paths=["src/billing/*"])
    assert dup["status"] == "duplicate", dup
    con = store.record_decision(a, "Use pymemcache for the cache", "it is simpler to run", components=["src/cache"],
                                chose=["pymemcache"])
    assert con["status"] == "conflict", con
    assert tools.check("src/billing/invoice.py", "total = float(x)\n", "")["verdict"] == "ask"  # agent trust -> ask


def test_bad_evidence_is_flagged(graph):
    r = store.record_decision(agent(), "Retry webhooks three times", "Partners time out at night, see the incident",
                              paths=["src/billing/webhooks.py"], evidence=["src/billing/nope.py:10", "commit:deadbeef"])
    assert r["status"] == "applied" and "Unverified evidence" in r["text"], r


def test_agent_cannot_promote_itself_and_human_approves(graph):
    r = store.record_decision(agent(), "Never hand-write migrations", "AGENTS.md says to run makemigrations",
                              paths=["*/migrations/*.py"], forbid=["any"], evidence=["AGENTS.md:2"], trust="documented")
    assert r["status"] == "pending", r
    assert tools.check("app/migrations/0002.py", "x = 1\n", "")["verdict"] == "ok"   # not active yet
    assert store.approve(agent(), r["id"])["status"] == "refused"                     # agents can't approve
    assert store.approve(human(), r["id"])["status"] == "applied"
    assert tools.check("app/migrations/0002.py", "x = 1\n", "")["verdict"] == "deny"  # documented -> deny


def test_supersede_needs_the_version_and_loses_races(graph):
    a, b = agent("s-a"), agent("s-b", "other-bot@example.com")
    did = store.record_decision(a, "Use orjson for API responses", "3x faster serialization on the hot path",
                                components=["src/billing"], chose=["orjson"])["id"]
    assert store.supersede(b, did, None, "x" * 6, "y" * 12)["status"] == "refused"      # no version
    ok = store.supersede(b, did, 1, "Use msgspec for API responses", "msgspec also validates, one dep less",
                         chose=["msgspec"], rejected=["orjson"])
    assert ok["status"] == "applied", ok
    late = store.supersede(a, did, 1, "Use ujson for API responses", "a stale writer tries to win")
    assert late["status"] == "stale" and "superseded by" in late["text"] and "other-bot" in late["text"], late
    assert "superseded by" in tools.why(did)


def test_protected_decision_becomes_a_proposal_then_revert(graph):
    h = human()
    did = store.record_decision(h, "Payments go through the billing service only", "PCI scope stays in one service",
                                components=["src/billing"])["id"]
    p = store.supersede(agent(), did, 1, "Let checkout call the payment gateway directly", "fewer hops, lower latency")
    assert p["status"] == "pending", p
    assert store.get(did)["status"] == "active"                  # still in force
    ap = store.approve(h, p["id"])
    assert ap["status"] == "applied" and store.get(did)["status"] == "superseded", ap
    rv = store.revert(h, ap["change"], "PCI review said no")
    assert rv["status"] == "applied", rv
    assert store.get(did)["status"] == "active" and store.get(p["id"])["status"] == "reverted"
    assert "valid_to" not in store.get(did)                      # open interval again


def test_locked_needs_owner_team(graph):
    h, outsider = human(), human("someone@example.com")
    did = store.record_decision(h, "Cache keys are namespaced per tenant", "tenant data leaked once",
                                components=["src/cache"])["id"]
    store.lock(h, did)
    store.team("platform", members=["asha@example.com"], owns=["src/cache"])
    p = store.retire(agent(), did, 2, "namespacing is slow")
    assert p["status"] == "pending", p
    db.write("MERGE (:Person {key: 'e:someone@example.com', name: 'someone', emails: ['someone@example.com']})")
    assert store.approve(outsider, p["id"])["status"] == "refused"
    assert store.approve(h, p["id"])["status"] == "applied"


def test_agent_revert_only_own_session_and_budget(graph):
    a = agent("s-budget")
    r = store.record_decision(a, "Log every webhook payload", "debugging partner issues needs the raw body",
                              paths=["src/billing/hooks.py"])
    assert store.revert(agent("s-other"), r["change"], "nope")["status"] == "refused"
    assert store.revert(a, r["change"], "my mistake")["status"] == "applied"
    old, config.WRITE_BUDGET = config.WRITE_BUDGET, 1
    try:
        assert store.record_decision(agent("s-budget"), "Another one here", "spam spam spam spam",
                                     paths=["x.py"])["status"] == "refused"
    finally:
        config.WRITE_BUDGET = old


def test_history_impact_and_readonly_query(graph):
    assert "billing" in tools.impact("src/cache")
    assert "C-" in tools.history(0)
    assert tools.query("MATCH (d:Decision) RETURN d.id")
    try:
        tools.query("CREATE (:X)")
        raise AssertionError("a write through query() must fail")
    except AssertionError:
        raise
    except Exception:
        pass
