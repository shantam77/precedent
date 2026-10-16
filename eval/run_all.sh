#!/usr/bin/env bash
# Every replay on one code version. Huge histories sample 40 guarded decisions for the resurrection test.
cd "$(dirname "$0")/.."
run() { echo "=== $1 $(date +%T)"; .venv/Scripts/python.exe -u -m eval.replay "../_data/$1" --cut "$2" --graph "replay_$1" --max-res "$3" > "eval/logs/replay_$1.log" 2>&1; }
run netbox 2024-01-01 0
run mitmproxy 2024-01-01 0
run paperless-ngx 2024-01-01 0
run wagtail 2024-01-01 0
run outline 2024-01-01 40
run saleor 2024-01-01 40
run sentry 2025-06-01 40
echo "all done $(date +%T)"
