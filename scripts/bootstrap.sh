#!/usr/bin/env bash
# Create the virtualenv and install sample-shop with its dev extras.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PYTHON_BIN="${PYTHON_BIN:-python}"

log "Creating virtualenv at ${REPO_ROOT}/.venv"
"${PYTHON_BIN}" -m venv "${REPO_ROOT}/.venv"

if [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
  VENV_PY="${REPO_ROOT}/.venv/Scripts/python.exe"
else
  VENV_PY="${REPO_ROOT}/.venv/bin/python"
fi

log "Installing sample-shop (editable, with dev extras)"
"${VENV_PY}" -m pip install --upgrade pip
"${VENV_PY}" -m pip install -e "${REPO_ROOT}/sample-shop[dev]"

if [[ ! -f "${REPO_ROOT}/.env" ]]; then
  log "Creating .env from .env.example"
  cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
fi

log "Done. Next: ./scripts/dev_up.sh"
