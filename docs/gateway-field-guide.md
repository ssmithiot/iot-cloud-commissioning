# Gateway Field Guide

For field technicians deploying, replacing, or recovering IoT Cx gateways. Assumes SSH access to the gateway (user `swadmin`) and a phone/laptop that can reach the cloud UI. You do NOT need engineering access, but some steps need someone with the cloud **admin token** — those are marked **[OFFICE]**.

Key paths on every gateway:
- Config: `/etc/iot-cx-agent/agent.yaml` · Secret: `/etc/iot-cx-agent/edge-agent.env` (contains `GATEWAY_API_TOKEN`; never copy it elsewhere)
- Data: `/var/lib/iot-cx-agent/` (SQLite queue and history)
- Code: `/home/swadmin/iot-cloud-commissioning` · Service: `iot-cx-agent.service`
- The agent uses BACnet port **47814** and must not touch the legacy 47808 runtime.

Quick health commands (use constantly):
```bash
systemctl status iot-cx-agent.service
journalctl -u iot-cx-agent.service -n 50 --no-pager
```
Healthy = log lines like `Heartbeat accepted for gateway GW###` every ~30 s.

---

## 1. New gateway (front-loading / first install)

1. **[OFFICE]** Provision in the cloud — records the gateway and returns the token **once**:
   `POST /api/admin/gateways/provision` with `{"gateway_id":"GW###","site_id":"<customer-site>","hostname":"gw###"}`.
   Office saves `gateway_api_token` into a file `gateway-token.env` containing exactly one line: `GATEWAY_API_TOKEN=iotcc_gw_...` and hands it to the technician by a secure channel (not email/chat).
2. On the gateway, install the software (once per image; cloned images already have it):
   ```bash
   cd /home/swadmin/iot-cloud-commissioning
   sudo scripts/install-edge-agent.sh
   ```
3. Provision the device:
   ```bash
   sudo scripts/provision-cloned-gateway.sh \
     --gateway-id GW### --site-id <customer-site> \
     --cloud-url https://iot-cloud-api-dev.onrender.com \
     --token-env-file /path/to/gateway-token.env
   ```
   The script writes config + secret, restarts the service, and never prints the token.
4. Delete the token file: `shred -u /path/to/gateway-token.env` (or securely delete on the transfer device).
5. **Verify** (§8).

Cloned-image fleets: prepare the master with `scripts/prepare-clone-master.sh` (see `docs/clone-safe-gateway-provisioning.md`), clone, then run only step 3–5 per unit. Never clone an already-provisioned gateway — tokens must be unique per gateway.

## 2. Replacement gateway (hardware swap)

1. Physically swap hardware; connect LAN. Reuse the same `gateway_id` and `site_id` — history and saved BACnet inventory stay attached to that ID.
2. **[OFFICE]** Re-provision the same gateway_id (`POST /api/admin/gateways/provision`, same body). This issues a **new** token. **Important:** the old token remains valid until an engineer revokes it manually (known gap — report the swap so it gets revoked; see §4).
3. On the new hardware: §1 steps 2–5 with the new token file.
4. Verify (§8). Saved devices/points reappear automatically; trends resume on the next cycle.

## 3. Gateway recovery (dead/corrupted software, hardware OK)

Try in this order; stop when healthy.

a. **Restart the service:** `sudo systemctl restart iot-cx-agent.service`, then check logs.
b. **Reinstall runtime, keep identity:** `sudo scripts/install-edge-agent.sh` preserves an existing `agent.yaml`, then restart. Config and token survive.
c. **Corrupt local database:** symptom `sqlite_db_ok: false` in logs/cloud UI. Stop service; move the data dir aside (`sudo mv /var/lib/iot-cx-agent /var/lib/iot-cx-agent.bad && sudo mkdir -p /var/lib/iot-cx-agent && sudo chown swadmin:swadmin /var/lib/iot-cx-agent`); start service. **Data loss note:** unsent trend samples in the old queue are lost — acceptable; cloud history is intact. Keep the `.bad` dir for diagnosis.
d. **Lost token** (`edge-agent.env` missing/damaged): treat as credential rotation (§4).
e. Full reimage: treat as replacement (§2).

## 4. Credential rotation (lost, suspected exposed, or scheduled)

1. **[OFFICE]** Re-provision the gateway_id to mint a new token (§2 step 2).
2. Install it on the gateway:
   ```bash
   sudo scripts/provision-cloned-gateway.sh --gateway-id GW### --site-id <site> \
     --cloud-url <cloud-url> --token-env-file /path/to/new-token.env
   ```
3. Verify heartbeat (§8), then destroy the token file.
4. **[OFFICE]** Revoke the old credential via the API:
   - List: `GET /api/admin/gateways/GW###/credentials` (admin token) — identify the old entry by `token_prefix`/`created_at`.
   - Revoke: `POST /api/admin/credentials/<credential_id>/revoke` — the old token stops working immediately.
   Every rotation MUST include this step, same day. If the token was *exposed* (not just lost), revoke FIRST (the gateway goes offline until step 2 completes).

## 5. Reprovisioning (move gateway to a different site/customer)

1. **[OFFICE]** Decide whether history should move. Provisioning with a new `site_id` re-points the same gateway record; heartbeat then keeps `site_id` in sync with the gateway's config.
2. On the gateway, run `provision-cloned-gateway.sh` with the new `--site-id` (and new token from the office provision call).
3. Old-site data (trends, jobs) stays under the gateway's history. If clean separation per customer is required, use a **new gateway_id** instead and treat it as a new gateway (§1) — ask the office which applies.

## 6. Software update

Run from the office (or any host with SSH to the gateway):
```bash
scripts/update-edge-agent.sh --host GW###.local --user swadmin --ref <tag-or-branch>
```
Updates repo + venv + systemd unit only; never touches the legacy 47808 runtime or the gateway's config/token. Details: `docs/field-gateway-update.md`.

Fleet rollout rule (from the release process): update 2–3 canary gateways, watch 24 h (heartbeats, trend backlog = 0, job success), then batches of ~20. The cloud "gateway updates" queue tracks who is done.

Queue-driven updates (UI multi-select → office upgrade webapp) now enforce a **post-update health gate**: an update only counts as completed after the gateway heartbeats back online with a healthy local database. The worker **halts automatically after 2 consecutive failures** (configurable via `IOT_EDGE_UPDATE_HALT_AFTER_FAILURES`) and leaves remaining updates unclaimed — investigate, then restart the webapp to resume. A "failed" update whose error mentions the health gate means the script ran but the gateway didn't come back healthy: treat as §9 troubleshooting, likely §7 rollback.

## 7. Rollback (edge)

The updater checks out any git ref, so rollback = update to the previous tag:
```bash
scripts/update-edge-agent.sh --host GW###.local --ref <previous-good-tag>
```
Then verify (§8). There is no automatic rollback — if a fleet batch goes bad, stop the batch, roll the affected gateways back with the same command, and report.

## 8. Expected verification (after ANY of the above)

On the gateway:
- [ ] `systemctl status iot-cx-agent.service` → active (running)
- [ ] `journalctl -u iot-cx-agent.service -n 20` → `Heartbeat accepted for gateway GW###`, no repeating errors
- [ ] No `gateway is unprovisioned` warnings (means config still has UNPROVISIONED placeholders)

In the cloud UI (any operator account):
- [ ] Gateway shows **online**, correct site, correct agent version
- [ ] Trend backlog fields near zero (`pending upload` count small and falling)
- [ ] If BACnet is connected: a point read job succeeds from the workspace

## 9. Common failures → operator actions

| Symptom | Likely cause | Action |
|---|---|---|
| Log: `Heartbeat returned HTTP 401` | Wrong/revoked token, or token file malformed | Confirm `edge-agent.env` has one `GATEWAY_API_TOKEN=iotcc_gw_...` line; if lost/mismatched → §4 rotation |
| Log: `Heartbeat returned HTTP 403` | Token belongs to a different gateway_id than agent.yaml | Fix `gateway_id` in `agent.yaml` or rotate with matching ID (§4) |
| Log: heartbeat connection errors | No internet/DNS, wrong `cloud_url` | `curl -s https://<cloud-url>/health` from the gateway; fix network or `cloud_url` in `agent.yaml`; restart service |
| `gateway is unprovisioned` | Provision script never ran | §1 step 3 |
| Cloud shows **stale/offline** but service running | Heartbeats failing (see above) or clock badly wrong | Check log errors first; verify `timedatectl` shows NTP-synced |
| `sqlite_db_ok: false` | Corrupt/failed local DB or permissions | §3c |
| Trend backlog climbing, heartbeat fine | Trend upload rejected (log shows trend errors) | Note the error text; retry is automatic with backoff; if 4xx persists, report to engineering — do not clear the queue unless instructed (§3c loses data) |
| BACnet reads fail, cloud fine | `bacwi/bacrp/bacrpm` paths, port conflict with legacy 47808 runtime, or LAN issue | Confirm tool paths in `agent.yaml`; confirm the agent uses 47814; test locally via the gateway's local UI |
| Tunnel not connected (cloud shows disconnected) | Tunnel disabled in config, or WebSocket blocked outbound | Heartbeat/jobs are separate from tunnel — the site may still be healthy. Check `tunnel_enabled` in config and site firewall for outbound WebSocket. Do NOT modify tunnel software — report if enabled-but-disconnected persists. |
| Update script fails mid-run | Network drop or git ref typo | Re-run with the same `--ref`; the script is repeatable. If the service won't start after: rollback (§7) and report. |

## 10. What technicians must never do
- Never copy a gateway token to another gateway, a personal device, email, or chat.
- Never edit tunnel software or cloud settings.
- Never clear `/var/lib/iot-cx-agent` on a healthy gateway (destroys unsent data).
- Never provision two gateways with the same `gateway_id`.
- Never point a production gateway at a staging cloud URL, or vice versa.
