#!/bin/sh
# One container: FalkorDB (from the falkordblite wheel) + the Precedent web/MCP server.
# On first boot it clones the demo repo at a fixed commit and builds its graph (code + git only, 0 LLM tokens).
set -e
BIN=$(python -c "import os, redislite; print(os.path.join(os.path.dirname(redislite.__file__), 'bin'))")
DATA=${DATA:-/data}; mkdir -p "$DATA"
"$BIN/redis-server" --port "${FALKOR_PORT:-6379}" --bind 127.0.0.1 --loadmodule "$BIN/falkordb.so" --dir "$DATA" \
  --daemonize yes --logfile "$DATA/falkordb.log"
until "$BIN/redis-cli" -p "${FALKOR_PORT:-6379}" ping >/dev/null 2>&1; do sleep 0.2; done
echo "FalkorDB up"

if ! "$BIN/redis-cli" -p "${FALKOR_PORT:-6379}" GRAPH.LIST | grep -qx "$PRECEDENT_GRAPH"; then
  [ -d "$DATA/repo"/.git ] || git clone --quiet "$DEMO_REPO" "$DATA/repo"
  git -C "$DATA/repo" checkout --quiet "$DEMO_SHA"
  python -m precedent --graph "$PRECEDENT_GRAPH" init "$DATA/repo"
fi

exec uvicorn precedent.web:app --host 0.0.0.0 --port "${PORT:-8000}"
