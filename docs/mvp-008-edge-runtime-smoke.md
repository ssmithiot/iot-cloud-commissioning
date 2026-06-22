# MVP-008 Edge Runtime Smoke

## Target

- Gateway ID: `GW006`
- Site ID: `DEV-CLONE-MASTER`
- Gateway LAN IP: `192.168.1.200`
- Access path: SSH to CP at `10.2.0.11`, then SSH to `swadmin@192.168.1.200`

## BACnet Runtime Rules

- Cloud commissioning BACnet jobs use UDP `47814`.
- Existing legacy runtime UDP `47808` must not be touched.
- Local commissioning UI has priority over cloud BACnet jobs.
- Shared BACnet lock path: `/tmp/iot-cloud-commissioning-bacnet-47814.lock`
- Cloud BACnet jobs must yield when `/tmp/iot-cloud-commissioning-bacnet-47814.lock` exists.
- Edge gateways continue to call FastAPI only and must not connect directly to Supabase or Postgres.

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
