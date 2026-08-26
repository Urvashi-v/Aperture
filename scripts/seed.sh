#!/usr/bin/env bash
# Load a dataset. Destructive: truncates every table first.
#   ./scripts/seed.sh              # profile from .env (SEED_PROFILE)
#   ./scripts/seed.sh medium       # explicit profile
#   ./scripts/seed.sh large 99     # explicit profile and random seed
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_venv
load_env

PROFILE="${1:-${SEED_PROFILE:-small}}"
SEED_VALUE="${2:-${SEED_RANDOM_SEED:-1337}}"

log "Seeding profile '${PROFILE}' (random seed ${SEED_VALUE})"
cd "${REPO_ROOT}/sample-shop"
"${VENV_PY}" -m shop.seed.cli --profile "${PROFILE}" --seed "${SEED_VALUE}"
