#!/usr/bin/env bash
# E2 on every repo in ../_data: replay real history after the cut through the guard. Sequential (8 GB laptop).
#   bash eval/run_replays.sh netbox mitmproxy saleor ...
cd "$(dirname "$0")/.."
mkdir -p eval/logs
for r in "$@"; do
  echo "=== $r $(date +%T)"
  .venv/Scripts/python.exe -u -m eval.replay "../_data/$r" --cut "${CUT:-2024-01-01}" --graph "replay_$r" > "eval/logs/replay_$r.log" 2>&1
  tail -n 40 "eval/logs/replay_$r.log" | head -n 32
done
echo "all done $(date +%T)"
