"""E1 on many repos: cold start each one and save what it found, so every mined decision can be checked by hand.

    python -m eval.coldstart ../_data/saleor ../_data/wagtail ...

Writes eval/out/coldstart_<repo>.json: the scan stats plus every decision with its commit (sha, subject, date).
"""

import json
import sys
from pathlib import Path

from precedent import config, db, scan

OUT = Path(__file__).parent / "out"


def main():
    for repo in sys.argv[1:]:
        repo = Path(repo).resolve()
        config.GRAPH, config.REPO = f"cs_{repo.name}", str(repo)
        db.graph.cache_clear()
        stats = scan.init(repo, name=config.GRAPH, verbose=False)
        rows = db.read("""MATCH (d:Decision)-[:EVIDENCED_BY]->(c:Commit)
            OPTIONAL MATCH (d)-[:CHOSE]->(l:Library)
            RETURN d.id, d.title, d.forbid, c.sha, c.subject, c.at, collect(l.name) ORDER BY c.at""")
        decisions = [{"id": i, "title": t, "forbid": f, "sha": s[:10], "subject": sub, "at": at, "chose": ch}
                     for i, t, f, s, sub, at, ch in rows]
        OUT.mkdir(exist_ok=True)
        (OUT / f"coldstart_{repo.name}.json").write_text(json.dumps({"stats": stats, "decisions": decisions}, indent=1))
        print(json.dumps(stats))


if __name__ == "__main__":
    main()
