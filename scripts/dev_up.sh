#!/usr/bin/env bash
# Start PostgreSQL and wait until it is genuinely accepting connections.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
load_env

log "Starting PostgreSQL"
docker compose -f "${REPO_ROOT}/docker-compose.yml" up -d postgres

log "Waiting for the health check to pass"
for _ in $(seq 1 60); do
  state="$(docker inspect --format '{{.State.Health.Status}}' aperture-postgres 2>/dev/null || echo starting)"
  if [[ "${state}" == "healthy" ]]; then
    echo "PostgreSQL is healthy on port ${POSTGRES_PORT:-5433}"
    exit 0
  fi
  sleep 1
done

echo "ERROR: PostgreSQL did not become healthy in 60s" >&2
docker compose -f "${REPO_ROOT}/docker-compose.yml" logs --tail 40 postgres >&2
exit 1
