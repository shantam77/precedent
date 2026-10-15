"""E4: latency of what hooks and agents call, on a big graph, at several sizes.

    python -m eval.bench --sizes 100,400,1600

For each size: build the synthetic world, then time check(), context(), impact(), why() on random files.
Hooks need check() well under 100 ms; the process start (~0.7 s Python + imports) dominates anyway.
"""

import argparse
import json
import random
import time
from pathlib import Path

from precedent import config, db, tools
from sim import world

OUT = Path(__file__).parent / "out"


def timed(fn, *args, n=300):
    xs = []
    for a in args[:n]:
        t = time.perf_counter()
        fn(*a)
        xs.append((time.perf_counter() - t) * 1000)
    xs.sort()
    return {"p50": round(xs[len(xs) // 2], 2), "p95": round(xs[int(len(xs) * .95)], 2), "max": round(xs[-1], 2)}


def memory_mb():
    """Memory of the whole FalkorDB server (other graphs are loaded too, so this is an upper bound)."""
    import redis
    return round(redis.Redis(host=config.HOST, port=config.PORT).info("memory")["used_memory"] / 2**20, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100,400,1600")
    a = ap.parse_args()
    rnd = random.Random(3)
    results = []
    for comps in [int(s) for s in a.sizes.split(",")]:
        graph = f"bench_{comps}"
        config.GRAPH = graph
        db.graph.cache_clear()
        tools._components = None
        w = world.build(components=comps, files_per=50, decisions=comps * 12, people=max(50, comps // 2),
                        sessions=comps * 5, graph=graph)
        files = rnd.sample(w["files"], 300)
        res = {"components": comps, "files": comps * 50, "decisions": comps * 12,
               "check_ms": timed(tools.check, *[(f, "import pymemcache\nx = float(y)\n", "") for f in files]),
               "context_ms": timed(lambda f: tools.context([f]), *[(f,) for f in files]),
               "impact_ms": timed(tools.impact, *[(f.rsplit("/", 1)[0],) for f in files[:100]]),
               "why_ms": timed(tools.why, *[(f"D-{rnd.randint(1, comps * 12)}",) for _ in range(100)]),
               "server_memory_mb": memory_mb()}
        res["edges"] = db.read("MATCH ()-[r]->() RETURN count(r)")[0][0]
        print(json.dumps(res))
        results.append(res)
        db.reset()  # free the WSL memory before the next size
    OUT.mkdir(exist_ok=True)
    (OUT / "bench.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
