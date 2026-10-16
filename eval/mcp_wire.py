"""Show the raw MCP traffic: start precedent.mcp_server over stdio and write JSON-RPC lines by hand (2026-07-28,
stateless: no initialize; every request carries its protocol version and capabilities in _meta).

    PRECEDENT_GRAPH=cs_netbox PRECEDENT_REPO=../_data/netbox python -m eval.mcp_wire
Writes eval/out/mcp_wire.txt (what went in, what came out).
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "wire-demo", "version": "0"}}
CALLS = [
    ("server/discover", {}),
    ("tools/list", {}),
    ("tools/call", {"name": "why", "arguments": {"target": "graphene-django"}}),
    ("tools/call", {"name": "check", "arguments": {"path": "netbox/netbox/graphql/schema.py", "new_text": "import graphene\n"}}),
]


def main():
    p = subprocess.Popen([sys.executable, "-m", "precedent.mcp_server"], cwd=ROOT, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    log = []
    for i, (method, params) in enumerate(CALLS, 1):
        msg = {"jsonrpc": "2.0", "id": i, "method": method, "params": {**params, "_meta": META}}
        line = json.dumps(msg)
        p.stdin.write(line + "\n")
        p.stdin.flush()
        reply = p.stdout.readline()
        log += [">>> " + line, "<<< " + reply.strip(), ""]
    p.stdin.close()
    p.wait(timeout=10)
    out = ROOT / "eval" / "out" / "mcp_wire.txt"
    out.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(l[:600] for l in log))


if __name__ == "__main__":
    main()
