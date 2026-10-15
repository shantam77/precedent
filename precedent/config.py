"""Settings from environment variables. Nothing else in the code reads os.environ."""

import os
import subprocess

HOST = os.getenv("FALKOR_HOST", "127.0.0.1")  # not "localhost": on Windows that tries IPv6 first (+2.5 s)
PORT = int(os.getenv("FALKOR_PORT", "6379"))
GRAPH = os.getenv("PRECEDENT_GRAPH", "precedent")      # one graph per repo
REPO = os.getenv("PRECEDENT_REPO", os.getcwd())        # repo root, for evidence checks
MODE = os.getenv("PRECEDENT_MODE", "on")               # on | off  (off = decoration test)
HARNESS = os.getenv("PRECEDENT_HARNESS", "cli")        # claude-code | cursor | cli | sim
FAIL_CLOSED = os.getenv("PRECEDENT_FAIL_CLOSED") == "1"
WRITE_BUDGET = int(os.getenv("PRECEDENT_WRITE_BUDGET", "25"))  # writes per agent session
TIMEOUT_MS = 2000


def actor_email():
    """Who is acting: PRECEDENT_ACTOR, else the git user of the repo."""
    if os.getenv("PRECEDENT_ACTOR"):
        return os.getenv("PRECEDENT_ACTOR")
    try:
        out = subprocess.run(["git", "config", "user.email"], cwd=REPO, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"
