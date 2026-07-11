# Legacy Edge Upgrade Webapp

This is a separate local-only tool for upgrading older edge-only gateways. It does not replace, rename, or change the existing IOTGWCFG / Gateway Update app.

## Start

Double-click:

```text
tools\start-legacy-edge-upgrade-webapp.cmd
```

Then open:

```text
http://127.0.0.1:8766
```

The local process also runs the cloud update worker. It polls the durable cloud
queue every 10 seconds by default, so an update requested from the cloud portal
normally changes from **Queued** to **Updating** within 10–30 seconds. No one
needs to re-enter the gateway ID or IP address in the local form.

To confirm that this machine is connected to the queue, open:

```text
http://127.0.0.1:8766/api/worker-status
```

`state: "polling"` means the worker authenticated to the cloud successfully.
If a second copy of the app is started, it now fails before starting a worker,
so it cannot create a hidden duplicate queue consumer.

## Local Defaults

The app reads form defaults from `.env` in the repo root, tool folder, or installed app folder:

```text
IOT_ADMIN_API_TOKEN=
CRADLEPOINT_PASSWORD=
GATEWAY_PASSWORD=
EDGE_UI_PASSWORD=
```

## What It Does

The app connects from the Windows machine through the Cradlepoint jump host to the gateway LAN SSH address:

```text
Windows app -> Cradlepoint SSH -> 192.168.1.200 gateway SSH
```

The mode is checkpoint/manual approval. It runs one phase, verifies it, then stops for approval before continuing. Dry-run mode shows the generated commands without connecting to the gateway or calling the cloud API.

## Phases

1. Inspect gateway
2. Back up local BACnet UI
3. Build/upload UI update ZIP
4. Apply UI update
5. Confirm UI auth
6. Restart local UI
7. Provision cloud gateway
8. Clone/update cloud repo
9. Write cloud config/token
10. Install Python agent
11. Install/start service
12. Final verification

## Safety Notes

- Passwords and tokens are held only in memory during the run.
- Logs redact the admin token, returned gateway token, Cradlepoint password, gateway SSH password, and local UI password.
- The returned gateway token is written only to `/etc/iot-cx-agent/edge-agent.env` on the gateway.
- `/etc/iot-cx-agent/edge-agent.env` is installed as `root:root` with mode `600`.
- The local BACnet UI is backed up before any UI files are changed.
- The UI update only replaces `app.py`, `templates/`, `README.md`, and `requirements.txt`.
- `start.sh`, `data/`, `static/`, and `edge-bacnet-ui.service` are preserved.
- Windows ZIP paths are normalized on extraction so `templates\base.html` is not created as a flat Linux filename.
- HTTP 302 to `/login` is treated as a pass because local UI auth is enabled.
- The cloud-agent pass condition is seeing `Heartbeat accepted` in the service log.

## Sample Sanitized Run Log

```text
=== Phase 7: Provision cloud gateway ===
Provisioning cloud gateway identity...
gateway_id: GW010
site_id: GW010
hostname: GW010
lan_ip: 192.168.1.200
bacnet_port: 47814
token_prefix: iotcc_gw_abc...
gateway token length: 65
Phase passed: Provision cloud gateway

=== Phase 9: Write cloud config/token ===
$ safe config verification
gateway_id: GW010
site_id: GW010
cloud_url: https://iot-cloud-api-dev.onrender.com
GATEWAY_API_TOKEN=***SET***
-rw------- 1 root root 65 ... /etc/iot-cx-agent/edge-agent.env
```

## Rollback

After a backup is created, the page enables a rollback button. Enter a backup filename such as:

```text
edge-bacnet-ui-v2.backup.20260706-141500.tar.gz
```

The app stops `edge-bacnet-ui.service`, moves the failed UI folder aside, restores the selected backup, fixes ownership, restarts the service, and checks `http://127.0.0.1:5000/`.

The page also includes a cloud-agent disable action:

```text
sudo systemctl stop iot-cx-agent.service
sudo systemctl disable iot-cx-agent.service
```

## Final Summary Report

At the end of the run the log prints:

- Gateway number
- Site ID
- Hostname
- Gateway LAN IP
- Local UI update status
- Local UI auth status
- Cloud provision status
- Cloud repo status
- Agent config status
- `iot-cx-agent` service status
- Heartbeat status
- Cloud portal manual confirmation
- Backup filename
- Any warnings/errors
