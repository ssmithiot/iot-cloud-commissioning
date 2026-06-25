# Field Gateway Update

MVP-011 supports a simple operator-driven update over SSH for gateways that have already been provisioned. This is not cloud-triggered self-update.

The update changes only:

- `/home/swadmin/iot-cloud-commissioning`
- `/home/swadmin/iot-cloud-commissioning/edge-agent/.venv`
- `/etc/systemd/system/iot-cx-agent.service`
- `iot-cx-agent.service`

It must not touch the legacy BACnet UDP `47808` runtime.

## Update Command

From an office workstation with SSH access:

```bash
cd /home/swadmin/iot-cloud-commissioning
scripts/update-edge-agent.sh --host GW007.local --user swadmin --ref main
```

To update to a specific tag, branch, or commit:

```bash
scripts/update-edge-agent.sh --host 192.168.1.50 --ref v0.1.1
```

The script performs these remote actions:

1. Fetches git branches and tags.
2. Checks out the requested ref.
3. Fast-forwards when the ref tracks a branch.
4. Creates or reuses `edge-agent/.venv`.
5. Installs `edge-agent/requirements.txt`.
6. Installs the edge agent package editable from `edge-agent`.
7. Reinstalls `deploy/iot-cx-agent.service`.
8. Restarts only `iot-cx-agent.service`.

## Verification

Check the service:

```bash
ssh swadmin@GW007.local 'sudo systemctl --no-pager --full status iot-cx-agent.service'
ssh swadmin@GW007.local 'journalctl -u iot-cx-agent.service -n 100 --no-pager'
```

Confirm the gateway is still configured for the cloud BACnet runtime:

```bash
ssh swadmin@GW007.local 'grep -E "gateway_id|site_id|cloud_url|bacnet_default_port|lock_path" /etc/iot-cx-agent/agent.yaml'
```

Queue a `bacnet_runtime_check` from the cloud API:

```bash
curl -X POST https://api.example.com/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW007","job_type":"bacnet_runtime_check","request":{"bacnet_port":47814}}'
```

Confirm heartbeat and job completion:

```bash
curl https://api.example.com/api/edge/gateways \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN"
curl https://api.example.com/api/edge/jobs \
  -H "Authorization: Bearer $IOT_ADMIN_API_TOKEN"
```

## Rollback

Use the same script with the previous known-good tag or commit:

```bash
scripts/update-edge-agent.sh --host GW007.local --ref v0.1.0
```

Then repeat service, heartbeat, and `bacnet_runtime_check` verification.

## Notes

- Do not copy `DATABASE_URL`, `GATEWAY_AUTH_PEPPER`, Supabase/Postgres credentials, or service-role keys to the gateway.
- Do not copy `IOT_ADMIN_API_TOKEN` to the gateway.
- Do not rotate or replace `/etc/iot-cx-agent/edge-agent.env` during a normal field update.
- Token replacement is an office-controlled provisioning/security operation, not part of the field update script.
- Cloud-triggered self-update and `agent_update` jobs are future work after signed versions, approvals, rollback rules, and stronger guardrails exist.
