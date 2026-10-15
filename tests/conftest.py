"""Tests run against a live FalkorDB (see README) on a throwaway graph, with a tiny git repo built on the fly."""

import os
import subprocess

import pytest

os.environ["PRECEDENT_GRAPH"] = "precedent_test"


def sh(repo, *args, date=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Asha K", "GIT_AUTHOR_EMAIL": "asha@example.com",
           "GIT_COMMITTER_NAME": "Asha K", "GIT_COMMITTER_EMAIL": "asha@example.com"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(args, cwd=repo, check=True, capture_output=True, env=env)


@pytest.fixture(scope="session")
def repo(tmp_path_factory):
    """shop/: the cache uses redis (pymemcache was replaced in a real commit), billing depends on the cache."""
    r = tmp_path_factory.mktemp("shop")
    sh(r, "git", "init", "-q", "-b", "main")
    for d in ("src/cache", "src/billing"):
        (r / d).mkdir(parents=True)
        (r / d / "__init__.py").write_text("")
    (r / "src/__init__.py").write_text("")
    (r / "requirements.txt").write_text("# Fast key-value cache\npymemcache\nrequests\n")
    (r / "src/cache/client.py").write_text("import pymemcache\n")
    (r / "src/billing/invoice.py").write_text("from src.cache import client\n# NOTE: amounts are integer cents, never floats\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "Initial shop", date="2026-01-05T10:00:00")
    (r / "requirements.txt").write_text("# Cache with pub/sub for notifications\nredis\nrequests\n")
    (r / "src/cache/client.py").write_text("import redis\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "Replace pymemcache with redis: notifications need pub/sub", date="2026-03-01T10:00:00")
    (r / "AGENTS.md").write_text("# Rules\n- Never write migrations by hand; run makemigrations instead.\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "Agent rules", date="2026-03-02T10:00:00")
    from precedent import config
    config.REPO = str(r)
    return r


@pytest.fixture(scope="session")
def graph(repo):
    from precedent import scan
    return scan.init(repo, name="shop", verbose=False)
