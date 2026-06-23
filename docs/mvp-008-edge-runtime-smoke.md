# MVP-008 Edge Runtime Smoke

## Target

- Gateway ID: `GW006`
- Site ID: `DEV-CLONE-MASTER`
- Gateway LAN IP: `192.168.1.200`
- Access path: SSH to CP at `10.2.0.11`, then SSH to `swadmin@192.168.1.200`

## BACnet Runtime Rules

- Cloud commissioning BACnet jobs use UDP `47814`.
- Existing legacy runtime UDP `47808` must not be touched.
- Shared BACnet lock path: `/tmp/iot-cloud-commissioning-bacnet-47814.lock`
- Local Edge BACnet Discovery & Commissioning UI owns UDP `47814` while actively running BACnet commands.
- Local commissioning UI has priority over cloud BACnet jobs.
- Cloud BACnet jobs must yield when `/tmp/iot-cloud-commissioning-bacnet-47814.lock` exists.
- Edge gateways continue to call FastAPI only and must not connect directly to Supabase or Postgres.

## GW006 Local UI Confirmed State

- `edge-bacnet-ui.service` is active/running.
- Flask UI URL: [http://192.168.1.200:5000/](http://192.168.1.200:5000/)
- `BACNET_IP_PORT=47814`
- Lock file is absent when the UI is idle.

## Live UI Patch Note

- The live GW006 UI patch is not part of this repo.
- The UI-side lock was added directly to `~/edge-bacnet-ui-v2/app.py` on GW006.
- The repo MVP-008 code only covers the cloud edge-agent yielding behavior.
- A future task should version-control/package the local UI lock patch.

## Runtime Check Job

Queue a runtime check from the cloud API:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/edge/jobs `
  -ContentType "application/json" `
  -Body '{"gateway_id":"GW006","job_type":"bacnet_runtime_check","request":{"port":47814}}'
```

The edge result reports the BACnet port, timeout, lock path, whether the lock is held, and whether `bacwi` and `bacrp` paths exist and are executable.

## Deferred Behavior

When the lock file exists, `bacnet_read` and `bacnet_discover` do not call BACnet subprocesses. They report:

```json
{
  "status": "deferred",
  "error": "bacnet_runtime_busy",
  "message": "Local commissioning UI is using BACnet port 47814. Cloud BACnet job yielded.",
  "lock_path": "/tmp/iot-cloud-commissioning-bacnet-47814.lock",
  "lock_held": true
}
```

## Stale Lock Recovery

Only remove the lock after confirming no `bacwi`, `bacrp`, `bacrpm`, or `bacwp` process is active.

Check active BACnet command processes:

```bash
ps aux | grep -Ei 'bacwi|bacrp|bacrpm|bacwp' | grep -v grep
```

If no BACnet command process is active, remove the stale lock:

```bash
rm -f /tmp/iot-cloud-commissioning-bacnet-47814.lock
```
