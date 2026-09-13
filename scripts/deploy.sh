#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SITE="${SITE_DIR:-$ROOT/site}"
if [[ ! -f "$SITE/index.html" ]]; then
  echo "missing $SITE/index.html — run: python3 -m op_usage generate (or demo)" >&2
  exit 1
fi
PROJECT="${CF_PAGES_PROJECT:-op-usage}"
exec npx --yes wrangler pages deploy "$SITE" --project-name "$PROJECT"
