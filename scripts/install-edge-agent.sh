#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/iot-cx-agent"
DATA_DIR="/var/lib/iot-cx-agent"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDGE_AGENT_DIR="${REPO_ROOT}/edge-agent"
SERVICE_SOURCE="${REPO_ROOT}/deploy/iot-cx-agent.service"
SERVICE_DEST="/etc/systemd/system/iot-cx-agent.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}"
chown swadmin:swadmin "${CONFIG_DIR}" "${DATA_DIR}"
chmod 750 "${CONFIG_DIR}" "${DATA_DIR}"

if [[ -f "${CONFIG_DIR}/agent.yaml" ]]; then
  echo "Existing ${CONFIG_DIR}/agent.yaml found; leaving it unchanged."
else
  cat > "${CONFIG_DIR}/agent.yaml" <<'YAML'
gateway_id: UNPROVISIONED
site_id: UNPROVISIONED
cloud_url: http://localhost:8000
bacnet_default_port: 47814
heartbeat_interval_sec: 30
agent_version: 0.1.0
ui_version: 0.1.0
bacnet:
  default_port: 47814
  bacwi_path: bacwi
  bacrp_path: bacrp
  lock_path: /tmp/iot-cloud-commissioning-bacnet-47814.lock
  timeout_sec: 10
YAML
  chown swadmin:swadmin "${CONFIG_DIR}/agent.yaml"
  chmod 640 "${CONFIG_DIR}/agent.yaml"
  echo "Created unprovisioned ${CONFIG_DIR}/agent.yaml. Run scripts/provision-cloned-gateway.sh before shipping."
fi

if [[ ! -f "${CONFIG_DIR}/edge-agent.env" ]]; then
  install -o swadmin -g swadmin -m 640 /dev/null "${CONFIG_DIR}/edge-agent.env"
fi

if [[ "${REPO_ROOT}" != "/home/swadmin/iot-cloud-commissioning" ]]; then
  echo "Warning: service file expects repo at /home/swadmin/iot-cloud-commissioning; current repo is ${REPO_ROOT}." >&2
fi

if [[ -d "${EDGE_AGENT_DIR}" ]]; then
  sudo -u swadmin python3 -m venv "${EDGE_AGENT_DIR}/.venv"
  sudo -u swadmin "${EDGE_AGENT_DIR}/.venv/bin/python" -m pip install -r "${EDGE_AGENT_DIR}/requirements.txt"
  sudo -u swadmin "${EDGE_AGENT_DIR}/.venv/bin/python" -m pip install -e "${EDGE_AGENT_DIR}"
else
  echo "Edge agent directory not found at ${EDGE_AGENT_DIR}; skipping Python install." >&2
fi

if [[ -f "${SERVICE_SOURCE}" ]]; then
  cp "${SERVICE_SOURCE}" "${SERVICE_DEST}"
  systemctl daemon-reload
  echo "Installed ${SERVICE_DEST}."
else
  echo "Service file not found at ${SERVICE_SOURCE}; skipping systemd install." >&2
fi

echo "Enable after provisioning with:"
echo "  systemctl enable --now iot-cx-agent.service"
