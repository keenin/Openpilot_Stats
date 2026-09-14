#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

CONFIG="${OP_USAGE_CONFIG:-$HOME/.config/op-usage/credentials.env}"
if [[ -f "$CONFIG" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG"
  set +a
fi

# 1) metadata / list  2) download missing qlogs  3) parse local + generate
python3 -m op_usage nightly --metadata-only -v
python3 -m op_usage sync-qlogs -v
python3 -m op_usage nightly -v
"$ROOT/scripts/deploy.sh"
