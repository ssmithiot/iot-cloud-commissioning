#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/iot-cx-agent"
CONFIG_FILE="${CONFIG_DIR}/agent.yaml"
ENV_FILE="${CONFIG_DIR}/edge-agent.env"
DATA_DIR="/var/lib/iot-cx-agent"
SERVICE_NAME="iot-cx-agent.service"
BACNET_PORT="47814"
LOCK_PATH="/tmp/iot-cloud-commissioning-bacnet-47814.lock"
RESTART_SERVICE="true"

usage() {
  cat <<'USAGE'
Usage:
  sudo scripts/provision-cloned-gateway.sh \
    --gateway-id GW007 \
    --site-id customer-site \
    --cloud-url https://api.example.com \
    --token-env-file /secure/path/gateway-token.env \
    [--hostname GW007] \
    [--bacwi-path /usr/local/bin/bacwi] \
    [--bacrp-path /usr/local/bin/bacrp] \
    [--bacrpm-path /usr/local/bin/bacrpm] \
    [--no-restart]

The token env file must contain GATEWAY_API_TOKEN=... and is never printed.
USAGE
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
  fi
}

GATEWAY_ID=""
SITE_ID=""
CLOUD_URL=""
TOKEN_ENV_FILE=""
HOSTNAME_VALUE=""
BACWI_PATH="bacwi"
BACRP_PATH="bacrp"
BACRPM_PATH="bacrpm"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gateway-id) GATEWAY_ID="$2"; shift 2 ;;
    --site-id) SITE_ID="$2"; shift 2 ;;
    --cloud-url) CLOUD_URL="$2"; shift 2 ;;
    --token-env-file) TOKEN_ENV_FILE="$2"; shift 2 ;;
    --hostname) HOSTNAME_VALUE="$2"; shift 2 ;;
    --bacwi-path) BACWI_PATH="$2"; shift 2 ;;
    --bacrp-path) BACRP_PATH="$2"; shift 2 ;;
    --bacrpm-path) BACRPM_PATH="$2"; shift 2 ;;
    --no-restart) RESTART_SERVICE="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

require_root

if [[ -z "${GATEWAY_ID}" || -z "${SITE_ID}" || -z "${CLOUD_URL}" || -z "${TOKEN_ENV_FILE}" ]]; then
  usage >&2
  exit 1
fi

if [[ ! "${GATEWAY_ID}" =~ ^GW[0-9]{3}$ ]]; then
  echo "--gateway-id must look like GW007." >&2
  exit 1
fi

if [[ "${SITE_ID}" =~ [[:space:]:#] ]]; then
  echo "--site-id must not contain whitespace, colon, or # characters." >&2
  exit 1
fi

if [[ "${CLOUD_URL}" =~ [[:space:]#] ]]; then
  echo "--cloud-url must not contain whitespace or # characters." >&2
  exit 1
fi

if [[ "${BACWI_PATH}" =~ [[:space:]#] || "${BACRP_PATH}" =~ [[:space:]#] || "${BACRPM_PATH}" =~ [[:space:]#] ]]; then
  echo "--bacwi-path, --bacrp-path, and --bacrpm-path must not contain whitespace or # characters." >&2
  exit 1
fi

if [[ ! -f "${TOKEN_ENV_FILE}" ]]; then
  echo "Token env file not found: ${TOKEN_ENV_FILE}" >&2
  exit 1
fi

if ! grep -q '^GATEWAY_API_TOKEN=.' "${TOKEN_ENV_FILE}"; then
  echo "Token env file must contain GATEWAY_API_TOKEN=..." >&2
  exit 1
fi

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}"
chown swadmin:swadmin "${CONFIG_DIR}" "${DATA_DIR}"
chmod 750 "${CONFIG_DIR}" "${DATA_DIR}"

cat > "${CONFIG_FILE}" <<YAML
gateway_id: ${GATEWAY_ID}
site_id: ${SITE_ID}
cloud_url: ${CLOUD_URL%/}
bacnet_default_port: ${BACNET_PORT}
heartbeat_interval_sec: 30
agent_version: 0.1.0
ui_version: 0.1.0
bacnet:
  default_port: ${BACNET_PORT}
  bacwi_path: ${BACWI_PATH}
  bacrp_path: ${BACRP_PATH}
  bacrpm_path: ${BACRPM_PATH}
  lock_path: ${LOCK_PATH}
  timeout_sec: 10
YAML

install -o swadmin -g swadmin -m 640 /dev/null "${ENV_FILE}"
grep -m 1 '^GATEWAY_API_TOKEN=' "${TOKEN_ENV_FILE}" > "${ENV_FILE}"
chown swadmin:swadmin "${CONFIG_FILE}" "${ENV_FILE}"
chmod 640 "${CONFIG_FILE}" "${ENV_FILE}"

if [[ -n "${HOSTNAME_VALUE}" ]]; then
  hostnamectl set-hostname "${HOSTNAME_VALUE}"
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
if [[ "${RESTART_SERVICE}" == "true" ]]; then
  systemctl restart "${SERVICE_NAME}"
fi

echo "Provisioned ${GATEWAY_ID}. Token installed at ${ENV_FILE}."
