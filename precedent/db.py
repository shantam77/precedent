"""FalkorDB access. write() for our own code, read() (read-only + timeout) for anything else."""

from functools import cache
from pathlib import Path

from falkordb import FalkorDB

from . import config

QUERIES = Path(__file__).parent / "queries"


@cache
def graph(name=None):
    return FalkorDB(host=config.HOST, port=config.PORT).select_graph(name or config.GRAPH)


def write(cypher, **params):
    return graph().query(cypher, params).result_set


def read(cypher, **params):
    return graph().ro_query(cypher, params, timeout=config.TIMEOUT_MS).result_set


@cache
def q(name):
    """A named query from precedent/queries/<name>.cypher (all core Cypher lives there)."""
    return (QUERIES / f"{name}.cypher").read_text(encoding="utf-8")


def bulk(cypher, rows, size=500):
    """UNWIND $rows in batches (cypher must start with UNWIND $rows AS r)."""
    for i in range(0, len(rows), size):
        write(cypher, rows=rows[i:i + size])


def reset():
    try:
        graph().delete()
    except Exception:
        pass
    graph.cache_clear()


# No index on Decision.status: the planner would start from "all active decisions" instead of the one component.
INDEXES = [
    ("Component", "name"), ("File", "path"), ("Library", "name"), ("Person", "key"), ("Team", "name"),
    ("Decision", "id"), ("Session", "id"), ("Change", "id"), ("Commit", "sha"),
    ("Repo", "name"), ("Note", "id"),
]


def create_indexes():
    for label, prop in INDEXES:
        try:
            write(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception:
            pass  # already exists
    try:
        write("CALL db.idx.fulltext.createNodeIndex('Decision', 'title', 'rationale')")
    except Exception:
        pass
