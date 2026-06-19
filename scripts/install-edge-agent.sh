#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/iot-cx-agent"
DATA_DIR="/var/lib/iot-cx-agent"
SERVICE_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/iot-cx-agent.service"
SERVICE_DEST="/etc/systemd/system/iot-cx-agent.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}"
chmod 750 "${CONFIG_DIR}" "${DATA_DIR}"

if [[ -f "${CONFIG_DIR}/agent.yaml" ]]; then
  echo "Existing ${CONFIG_DIR}/agent.yaml found; leaving it unchanged."
else
  cat > "${CONFIG_DIR}/agent.yaml" <<'YAML'
gateway_id: GW001
site_id: demo-site
cloud_url: http://localhost:8000
bacnet_default_port: 47814
heartbeat_interval_sec: 30
agent_version: 0.1.0
ui_version: 0.1.0
YAML
  chmod 640 "${CONFIG_DIR}/agent.yaml"
  echo "Created ${CONFIG_DIR}/agent.yaml. Review gateway_id, site_id, and cloud_url before starting."
fi

if [[ -f "${SERVICE_SOURCE}" ]]; then
  cp "${SERVICE_SOURCE}" "${SERVICE_DEST}"
  systemctl daemon-reload
  echo "Installed ${SERVICE_DEST}."
else
  echo "Service file not found at ${SERVICE_SOURCE}; skipping systemd install." >&2
fi

echo "Install the Python package from edge-agent, then enable with:"
echo "  systemctl enable --now iot-cx-agent.service"

