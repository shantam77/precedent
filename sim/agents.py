"""E3: many agents and people writing to ONE shared graph at the same time, with mistakes on purpose.

8 agent threads (different people, harnesses and sessions) + 2 human threads run against the synthetic graph.
Each agent op is labelled with what SHOULD happen, so we can count what actually happened:

  record_new        a genuinely new decision                          -> applied
  record_duplicate  re-records an existing decision in other words    -> refused (duplicate)
  record_conflict   chooses a library an active decision rejected     -> refused (conflict)
  supersede_low     replaces an agent/historical decision, right ver  -> applied (or stale if someone was faster)
  supersede_stale   uses an old version number                        -> refused (stale)
  overwrite_high    tries to replace a human/documented/locked one    -> pending proposal, NEVER applied
  self_approve      tries to approve its own proposal                 -> refused
  revert_foreign    tries to undo another session's change            -> refused
  check_violation   an edit that adds a forbidden library/pattern      -> ask or deny (by trust)
Humans: approve some proposals, reject others, revert some approved changes.

Hot spots on purpose: 30% of supersedes target the same 25 decisions, so agents race each other.
After the run, invariants are checked against the graph itself.

    python -m sim.agents --graph synthetic --ops 150
"""

import argparse
import json
import random
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from precedent import config, db, store, tools

OUT = Path(__file__).resolve().parent.parent / "eval" / "out"
MIX = [("record_new", 14), ("record_duplicate", 8), ("record_conflict", 8), ("supersede_low", 18), ("supersede_stale", 6),
       ("overwrite_high", 14), ("self_approve", 4), ("revert_foreign", 4), ("check_violation", 24)]
EXPECT = {"record_new": {"applied"}, "record_duplicate": {"duplicate"}, "record_conflict": {"conflict"},
          "supersede_low": {"applied", "stale"}, "supersede_stale": {"stale"}, "overwrite_high": {"pending", "stale"},
          "self_approve": {"refused"}, "revert_foreign": {"refused"}, "check_violation": {"ask", "deny"}}


def pick_decision(rnd, low, hot):
    """A random active decision; low=True -> trust <= agent and unlocked. 30% of the time from a small hot set."""
    if rnd.random() < 0.3 and hot:
        return rnd.choice(hot)
    trusts = ["observed", "historical", "agent"] if low else ["documented", "human"]
    rows = db.read("""MATCH (d:Decision {status: 'active'}) WHERE d.trust IN $t AND d.locked = $lk AND size(d.paths) = 0
        WITH d SKIP $skip LIMIT 1 MATCH (d)-[:ABOUT]->(c:Component)
        RETURN d.id, d.version, c.name, [(d)-[:REJECTED]->(r1:Library) | r1.name], d.title""",
                   t=trusts, lk=(not low and rnd.random() < 0.3), skip=rnd.randint(0, 800))
    return rows[0] if rows else None


def agent_loop(i, ops, seed, files, log, hot, lock):
    rnd = random.Random(seed + i)
    me = store.Actor(f"agent{i}@corp.example", "agent", rnd.choice(["claude-code", "cursor"]), f"sim-a{i}-{seed}")
    my_changes, my_proposals = [], []
    kinds, weights = zip(*MIX)
    for n in range(ops):
        kind = rnd.choices(kinds, weights)[0]
        t = time.perf_counter()
        try:
            if kind == "record_new":
                f = rnd.choice(files)
                comp = f.rsplit("/", 1)[0]
                r = store.record_decision(me, f"Use feature flag {rnd.randint(0, 10**9)} rollout for {comp}",
                                          f"Agent {i} chose a staged rollout because {rnd.random():.6f} of users saw errors",
                                          paths=[f], evidence=[f"user: rollout requested in ticket {rnd.randint(1, 9999)}"])
                outcome = r["status"]
                if r.get("change"):
                    my_changes.append(r["change"])
            elif kind == "record_duplicate":
                d = pick_decision(rnd, True, None)
                r = store.record_decision(me, d[4], "the same choice again, recorded twice by mistake", components=[d[2]])
                outcome = r["status"]
            elif kind == "record_conflict":
                d = pick_decision(rnd, rnd.random() < 0.5, None)
                if not d or not d[3]:
                    continue
                r = store.record_decision(me, f"Switch {d[2]} to {d[3][0]}", f"{d[3][0]} looked simpler to agent {i}",
                                          components=[d[2]], chose=[d[3][0]])
                outcome = r["status"]
            elif kind in ("supersede_low", "supersede_stale"):
                d = pick_decision(rnd, True, hot)
                if not d:
                    continue
                ver = db.read("MATCH (x:Decision {id: $id}) RETURN x.version, x.status", id=d[0])[0]
                v = ver[0] - 1 if kind == "supersede_stale" and ver[0] > 1 else (ver[0] + 5 if kind == "supersede_stale" else ver[0])
                time.sleep(rnd.random() * 0.01)  # think time between read and write: where races happen
                r = store.supersede(me, d[0], v, f"Revise {d[0]} in {d[2]} ({rnd.randint(0, 10**6)})",
                                    f"agent {i} found a better option during task {n}")
                outcome = r["status"]
                if r.get("change"):
                    my_changes.append(r["change"])
            elif kind == "overwrite_high":
                d = pick_decision(rnd, False, None)
                if not d:
                    continue
                ver = db.read("MATCH (x:Decision {id: $id}) RETURN x.version", id=d[0])[0][0]
                r = store.supersede(me, d[0], ver, f"Replace protected {d[0]} ({rnd.randint(0, 10**6)})",
                                    f"agent {i} thinks this rule is in the way of task {n}")
                outcome = r["status"]
                if r["status"] == "pending":
                    my_proposals.append(r["id"])
                    with lock:
                        log["proposals"].append(r["id"])
            elif kind == "self_approve":
                if not my_proposals:
                    continue
                outcome = store.approve(me, rnd.choice(my_proposals))["status"]
            elif kind == "revert_foreign":
                other = db.read("MATCH (ch:Change) WHERE ch.session <> $s AND ch.op IN ['record', 'supersede'] "
                                "RETURN ch.id SKIP $k LIMIT 1", s=me.session, k=rnd.randint(0, 50))
                if not other:
                    continue
                outcome = store.revert(me, other[0][0], "agent tries to undo someone else's work")["status"]
            else:  # check_violation
                d = db.read("""MATCH (d:Decision {status: 'active'})-[:ABOUT]->(c:Component) WHERE size(d.forbid) > 0
                    WITH d, c SKIP $k LIMIT 1 MATCH (f:File)-[:IN]->(c)
                    RETURN d.forbid[0], f.path, d.id LIMIT 1""", k=rnd.randint(0, 3000))
                if not d:
                    continue
                tok, path, did = d[0]
                text = f"import {tok[4:]}\n" if tok.startswith("lib:") else f"x = {tok}1)\n"
                outcome = tools.check(path, text, "")["verdict"]
                if outcome == "ok" and db.read("MATCH (d:Decision {id: $id}) RETURN d.status", id=did)[0][0] != "active":
                    outcome = "ask"  # another agent replaced that decision between our read and the check: correct
        except Exception as e:  # count, don't crash the run
            outcome = f"error: {type(e).__name__}: {e}"[:120]
        ms = (time.perf_counter() - t) * 1000
        with lock:
            log["ops"].append({"agent": i, "kind": kind, "outcome": outcome, "ok": outcome in EXPECT[kind], "ms": ms})


def human_loop(j, rounds, seed, log, lock, stop):
    rnd = random.Random(seed * 31 + j)
    me = store.Actor(f"dev{j}@corp.example", "human", "cli", f"sim-h{j}-{seed}")
    while not stop.is_set() or rounds > 0:
        rounds -= 1
        time.sleep(0.05)
        with lock:
            props = list(log["proposals"])
        if not props:
            if stop.is_set():
                break
            continue
        pid = rnd.choice(props)
        t = time.perf_counter()
        if rnd.random() < 0.5:
            r = store.approve(me, pid)
            kind = "human_approve"
            if r["status"] == "applied" and rnd.random() < 0.4:
                r2 = store.revert(me, r["change"], "approved too fast, rolling back")
                with lock:
                    log["ops"].append({"agent": f"h{j}", "kind": "human_revert", "outcome": r2["status"], "ok": r2["status"] == "applied",
                                       "ms": 0, "change": r["change"]})
        else:
            r = store.reject(me, pid, "not convinced")
            kind = "human_reject"
        with lock:
            log["ops"].append({"agent": f"h{j}", "kind": kind, "outcome": r["status"], "ok": True,
                               "ms": (time.perf_counter() - t) * 1000})
            if pid in log["proposals"]:
                log["proposals"].remove(pid)


def invariants():
    """Checked on the graph after the run. Every number here must be 0."""
    q = {
        # a protected decision ended without a human approving it
        "protected_ended_without_human": """MATCH (d:Decision) WHERE (d.trust IN ['documented', 'human'] OR d.locked)
            AND d.status IN ['superseded', 'retired'] AND d.valid_to >= $t0
            OPTIONAL MATCH (ch:Change)-[:ON]->(d) WHERE ch.kind = 'human'
            WITH d, count(ch) AS humans WHERE humans = 0 RETURN count(d)""",
        # two live successors for one decision (a lost race that both sides think they won)
        "double_successors": """MATCH (n:Decision)-[:SUPERSEDES]->(o:Decision) WHERE n.status = 'active'
            WITH o, count(n) AS k WHERE k > 1 RETURN count(o)""",
        # an applied supersede/retire without a Change node explaining it
        "unlogged_endings": """MATCH (d:Decision) WHERE d.status IN ['superseded', 'retired']
            AND d.valid_to >= $t0 AND NOT (d)<-[:ON]-(:Change) RETURN count(d)""",
        # a superseded decision with no end date (history broken)
        "ended_without_valid_to": "MATCH (d:Decision) WHERE d.status IN ['superseded', 'retired', 'reverted'] AND d.valid_to IS NULL RETURN count(d)",
    }
    return {k: db.read(v, t0=START)[0][0] for k, v in q.items()}


START = 0


def main():
    global START
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default="synthetic")
    ap.add_argument("--agents", type=int, default=8)
    ap.add_argument("--humans", type=int, default=2)
    ap.add_argument("--ops", type=int, default=150)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    config.GRAPH = a.graph
    config.WRITE_BUDGET = 10**6
    START = int(time.time() * 1000)
    files = [f for (f,) in db.read("MATCH (f:File) RETURN f.path")]
    hot = [r for r in (db.read("""MATCH (d:Decision {status: 'active'}) WHERE d.trust IN ['historical', 'agent'] AND NOT d.locked
        AND size(d.paths) = 0 WITH d SKIP 100 LIMIT 25 MATCH (d)-[:ABOUT]->(c:Component)
        RETURN d.id, d.version, c.name, [(d)-[:REJECTED]->(r1:Library) | r1.name], d.title"""))]
    before = db.read("MATCH (d:Decision) RETURN count(d)")[0][0]
    log, lock, stop = {"ops": [], "proposals": []}, threading.Lock(), threading.Event()
    t0 = time.time()
    ts = [threading.Thread(target=agent_loop, args=(i, a.ops, a.seed, files, log, hot, lock)) for i in range(a.agents)]
    hs = [threading.Thread(target=human_loop, args=(j, 10**9, a.seed, log, lock, stop)) for j in range(a.humans)]
    for t in ts + hs:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    for t in hs:
        t.join(timeout=30)
    wall = time.time() - t0
    after = db.read("MATCH (d:Decision) RETURN count(d)")[0][0]
    by = defaultdict(Counter)
    lat = defaultdict(list)
    for o in log["ops"]:
        by[o["kind"]][o["outcome"] if not o["outcome"].startswith("error") else "error"] += 1
        lat[o["kind"]].append(o["ms"])
    agent_ops = [o for o in log["ops"] if isinstance(o["agent"], int)]
    wrong = [o for o in agent_ops if not o["ok"]]
    report = {
        "graph": a.graph, "agents": a.agents, "humans": a.humans, "agent_ops": len(agent_ops),
        "human_ops": len(log["ops"]) - len(agent_ops), "wall_seconds": round(wall, 1),
        "ops_per_second": round(len(log["ops"]) / wall, 1),
        "outcomes": {k: dict(v) for k, v in by.items()},
        "unexpected_outcomes": len(wrong), "unexpected_examples": wrong[:10],
        "latency_ms_p50_p95": {k: [round(sorted(v)[len(v) // 2], 1), round(sorted(v)[int(len(v) * .95)], 1)] for k, v in lat.items() if v},
        "decisions_before": before, "decisions_after": after, "deleted_decisions": max(0, before - after),
        "invariants (all must be 0)": invariants(),
        "races_lost_cleanly (stale on supersede_low)": by["supersede_low"]["stale"],
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "multi_agent.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != "unexpected_examples"}, indent=1, default=str))
    for w in wrong[:10]:
        print("UNEXPECTED", w)


if __name__ == "__main__":
    main()
