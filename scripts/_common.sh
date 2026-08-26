#!/usr/bin/env bash
# Shared helpers for the scripts in this directory. Not executable on its own.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

# The virtualenv layout differs between platforms: Scripts/ on Windows,
# bin/ everywhere else. Resolve it once rather than in every script.
if [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
  VENV_PY="${REPO_ROOT}/.venv/Scripts/python.exe"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  VENV_PY="${REPO_ROOT}/.venv/bin/python"
else
  VENV_PY=""
fi
export VENV_PY

require_venv() {
  if [[ -z "${VENV_PY}" ]]; then
    echo "ERROR: no virtualenv found at ${REPO_ROOT}/.venv" >&2
    echo "Run: ./scripts/bootstrap.sh" >&2
    exit 1
  fi
}

# Load .env so the scripts and the application agree on ports and credentials.
load_env() {
  if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "${REPO_ROOT}/.env"
    set +a
  fi
}

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
