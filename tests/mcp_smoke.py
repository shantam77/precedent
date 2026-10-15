"""Start the MCP server as a real subprocess over stdio, list its tools, call a read and a write.

    PRECEDENT_GRAPH=<graph> PRECEDENT_REPO=<repo> python tests/mcp_smoke.py
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "precedent.mcp_server"], cwd=str(ROOT),
                                   env={**os.environ, "PRECEDENT_HARNESS": "mcp-smoke"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            for name, args in [("why", {"target": "graphene-django"}), ("impact", {"target": "netbox/utilities"}),
                               ("check", {"path": "netbox/netbox/graphql/schema.py", "new_text": "import graphene\n"})]:
                res = await s.call_tool(name, args)
                print(f"{name} ->", res.content[0].text[:200].replace("\n", " | "))


asyncio.run(main())
