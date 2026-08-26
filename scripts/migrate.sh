#!/usr/bin/env bash
# Bring the application database up to the latest migration.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_venv
load_env

log "alembic upgrade head"
cd "${REPO_ROOT}/sample-shop"
"${VENV_PY}" -m alembic upgrade head

log "Current revision"
"${VENV_PY}" -m alembic current
