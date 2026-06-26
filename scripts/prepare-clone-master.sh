#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/iot-cx-agent"
CONFIG_FILE="${CONFIG_DIR}/agent.yaml"
ENV_FILE="${CONFIG_DIR}/edge-agent.env"
DATA_DIR="/var/lib/iot-cx-agent"
SERVICE_NAME="iot-cx-agent.service"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
  fi
}

require_root

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}"
chown swadmin:swadmin "${CONFIG_DIR}" "${DATA_DIR}"
chmod 750 "${CONFIG_DIR}" "${DATA_DIR}"

cat > "${CONFIG_FILE}" <<'YAML'
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
  bacrpm_path: bacrpm
  lock_path: /tmp/iot-cloud-commissioning-bacnet-47814.lock
  timeout_sec: 10
YAML

install -o swadmin -g swadmin -m 640 /dev/null "${ENV_FILE}"
chown swadmin:swadmin "${CONFIG_FILE}"
chmod 640 "${CONFIG_FILE}"

rm -f "${DATA_DIR}/edge.db"
rm -f /tmp/iot-cloud-commissioning-bacnet-47814.lock

if grep -R -E 'GW006|GATEWAY_API_TOKEN=|DATABASE_URL|GATEWAY_AUTH_PEPPER|SERVICE_ROLE|service-role|SUPABASE|POSTGRES|postgres://' "${CONFIG_DIR}" >/dev/null 2>&1; then
  echo "Clone master check failed: forbidden identity or cloud credential material found in ${CONFIG_DIR}." >&2
  exit 1
fi

echo "Clone master prepared in UNPROVISIONED state. No gateway token is installed."
