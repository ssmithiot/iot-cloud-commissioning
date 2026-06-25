#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST=""
SSH_USER="swadmin"
GIT_REF="main"
REMOTE_REPO="/home/swadmin/iot-cloud-commissioning"
SERVICE_NAME="iot-cx-agent.service"

usage() {
  cat <<'USAGE'
Usage:
  scripts/update-edge-agent.sh --host GW007.local [--user swadmin] [--ref v0.1.0] [--repo /home/swadmin/iot-cloud-commissioning]

Updates only the cloud commissioning repo, edge-agent virtualenv, and iot-cx-agent.service.
It does not touch the legacy BACnet UDP 47808 runtime.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) REMOTE_HOST="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --ref) GIT_REF="$2"; shift 2 ;;
    --repo) REMOTE_REPO="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${REMOTE_HOST}" ]]; then
  usage >&2
  exit 1
fi

ssh "${SSH_USER}@${REMOTE_HOST}" bash -s -- "${REMOTE_REPO}" "${GIT_REF}" "${SERVICE_NAME}" <<'REMOTE'
set -euo pipefail

REMOTE_REPO="$1"
GIT_REF="$2"
SERVICE_NAME="$3"

cd "${REMOTE_REPO}"
git fetch --all --tags --prune
git checkout "${GIT_REF}"
git pull --ff-only origin "${GIT_REF}" 2>/dev/null || true

cd "${REMOTE_REPO}/edge-agent"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .

sudo install -m 0644 "${REMOTE_REPO}/deploy/iot-cx-agent.service" /etc/systemd/system/iot-cx-agent.service
sudo systemctl daemon-reload
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl --no-pager --full status "${SERVICE_NAME}"
REMOTE
