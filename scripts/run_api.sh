#!/usr/bin/env bash
# Run the sample-shop API in the foreground.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_venv
load_env

cd "${REPO_ROOT}/sample-shop"
exec "${VENV_PY}" -m uvicorn shop.main:app \
  --host "${SHOP_HOST:-0.0.0.0}" \
  --port "${SHOP_PORT:-8000}" \
  "$@"
