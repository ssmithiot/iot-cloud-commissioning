# Clone-Safe Gateway Provisioning

MVP-011 makes the gateway image safe to clone in the office before a unit ships. The clone master must boot into an explicit `UNPROVISIONED` state, must not contain any gateway token, and must not contain cloud/server credentials.

## Security Boundary

The edge gateway calls FastAPI only. The gateway may receive a single gateway API token in `/etc/iot-cx-agent/edge-agent.env`.

Never install these values on an edge gateway:

- `DATABASE_URL`
- `GATEWAY_AUTH_PEPPER`
- Supabase or Postgres credentials
- Supabase anon keys or service-role keys

The server-side pepper stays in the cloud API environment. Gateway credential creation remains a cloud/office operation.

## Runtime Boundary

Cloud commissioning BACnet jobs use UDP `47814`.

The legacy UDP `47808` runtime must not be stopped, reconfigured, restarted, or overwritten by clone preparation, office provisioning, or field update scripts.

The shared cloud commissioning BACnet lock path is:

```text
/tmp/iot-cloud-commissioning-bacnet-47814.lock
```

## Clone Master Preparation

Run this on the master gateway image before cloning:

```bash
cd /home/swadmin/iot-cloud-commissioning
sudo scripts/install-edge-agent.sh
sudo scripts/prepare-clone-master.sh
```

`prepare-clone-master.sh`:

- Stops and disables `iot-cx-agent.service`.
- Writes `/etc/iot-cx-agent/agent.yaml` with `gateway_id: UNPROVISIONED`.
- Creates an empty `/etc/iot-cx-agent/edge-agent.env`.
- Removes the local edge-agent SQLite database.
- Removes only the cloud commissioning BACnet lock file for UDP `47814`.
- Checks `/etc/iot-cx-agent` for `GW006`, `GATEWAY_API_TOKEN=`, `DATABASE_URL`, `GATEWAY_AUTH_PEPPER`, service-role strings, and Supabase/Postgres credential markers.

Before capturing the clone master, set the OS hostname to a generic image name such as `iot-cx-unprovisioned` or record that the first office provisioning step must set it with `--hostname GW0xx`. The provisioning script can set the final hostname, but the clone master should not be captured with a shipped gateway hostname.

The unprovisioned agent is safe if booted before provisioning. It initializes local state, logs that provisioning is missing, and skips heartbeat and job polling until a real gateway ID, site ID, and token are installed.

## Office Provisioning

Provisioning happens before shipping. Create the gateway identity and token in the cloud API from an office machine or trusted server-side environment:

```bash
cd cloud-api
python scripts/create_gateway_credential.py GW007 --label "GW007 edge agent"
```

Save the printed token once into a temporary root-readable file on the gateway:

```bash
sudo install -m 600 /dev/null /root/GW007.edge-agent.env
sudoedit /root/GW007.edge-agent.env
```

The file must contain:

```text
GATEWAY_API_TOKEN=iotcc_gw_<token_prefix>_<secret>
```

Then provision the cloned gateway:

```bash
cd /home/swadmin/iot-cloud-commissioning
sudo scripts/provision-cloned-gateway.sh \
  --gateway-id GW007 \
  --site-id customer-site \
  --hostname GW007 \
  --cloud-url https://api.example.com \
  --token-env-file /root/GW007.edge-agent.env \
  --bacwi-path /usr/local/bin/bacwi \
  --bacrp-path /usr/local/bin/bacrp
```

The script writes gateway identity and BACnet settings to `/etc/iot-cx-agent/agent.yaml`. It installs the token only at:

```text
/etc/iot-cx-agent/edge-agent.env
```

The token is not printed back to logs. Both config files are owned by `swadmin:swadmin` with mode `640`, and `/etc/iot-cx-agent` is mode `750`.

After provisioning, delete the temporary token file:

```bash
sudo shred -u /root/GW007.edge-agent.env
```

If `shred` is unavailable, remove the file with `sudo rm -f /root/GW007.edge-agent.env` and confirm it is gone.

## Office Verification

Confirm the service is running:

```bash
sudo systemctl --no-pager --full status iot-cx-agent.service
journalctl -u iot-cx-agent.service -n 100 --no-pager
```

Confirm heartbeat in the cloud API:

```bash
curl https://api.example.com/api/edge/gateways
```

Queue a BACnet runtime check from the cloud side:

```bash
curl -X POST https://api.example.com/api/edge/jobs \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW007","job_type":"bacnet_runtime_check","request":{"port":47814}}'
```

Then confirm the job result:

```bash
curl https://api.example.com/api/edge/jobs
```

When a known BACnet device is available on the office bench, queue a smoke read:

```bash
curl -X POST https://api.example.com/api/edge/jobs \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":"GW007","job_type":"bacnet_read","request":{"device_instance":1234,"object_type":"analog-value","object_instance":1,"property":"present-value"}}'
```

Capture the gateway label, NIC1 MAC, IP address, modem/router information, cloud heartbeat evidence, BACnet runtime check result, smoke-test result when available, and screenshots/docs in the QC packet before shipping.

## Pre-Ship Checklist

- Gateway has a unique `GW0xx` ID.
- Hostname matches the assigned gateway ID or office naming plan.
- `/etc/iot-cx-agent/agent.yaml` does not contain `GW006`.
- `/etc/iot-cx-agent/edge-agent.env` contains only this gateway's token.
- No clone ships with GW006's active token.
- No edge file contains `DATABASE_URL`, `GATEWAY_AUTH_PEPPER`, Supabase/Postgres credentials, or service-role keys.
- Cloud BACnet port is `47814`.
- Lock path is `/tmp/iot-cloud-commissioning-bacnet-47814.lock`.
- Legacy UDP `47808` runtime was not touched.

## Future Work

Automatic enrollment, cloud-triggered self-update, signed agent versions, rollback rules, and update approvals are intentionally outside MVP-011.
