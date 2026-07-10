# Project Handoff

Last updated: 2026-07-10

## Current Focus

We are working on the cloud point workspace / KMC-style point table and BACnet value refresh from edge gateways.

The desired architecture is:

- Cloud UI requests a table/controller value refresh.
- Cloud queues one `bacnet_read_bulk` job per BACnet controller/device, not one cloud job per point.
- Edge agent performs the BACnet reads locally.
- Edge agent posts one completed job result back to cloud with a bulk `values[]` payload.
- Cloud persists those values onto saved points so the tree and table show last known values immediately.

## Product Principle: Do Not Release Slow, Untrusted Cloud Values

This is probably the hardest part of the product to get right. Most edge-to-cloud graphics systems are slow, stale, or unclear enough that operators stop trusting the values. We are not releasing another system like that.

The north star:

- The value on screen must be trusted, or clearly labeled as not fresh/trustworthy.
- BACnet reads should happen close to the wire on the edge.
- Cloud should request, supervise, visualize, trend, and persist.
- Cloud should show last-known values immediately, but always make freshness, source, and failures visible.
- Values should eventually carry metadata: read timestamp, age, source, quality/status, and failure reason.
- If a read fails, the UI must say what failed and what value is still being shown.
- Trending is part of trust, not a luxury feature.

We have come a long way quickly, but this cannot suck like the other edge-to-cloud systems. If the values are slow or untrusted, we keep iterating. No release until the behavior earns confidence.

## Latest Pushed Commits

### End-of-day status — 2026-07-10

Today’s major milestone is complete: edge RPM now performs bulk BACnet reads locally, packages the results, and returns them to the cloud in under a minute on live gateways.

Verified live gateways:

- GW036: combined legacy update completed; agent heartbeat and outbound tunnel verified; cloud values refreshed successfully.
- GW047: combined legacy update completed end-to-end; UI, agent, service, heartbeat, and final verification passed.
- GW044: BACnet/IP controller UI RPM refresh works quickly; cloud bulk-read validation identified a `schedule` point type and production support was added.

Production `main` is currently at:

- `51ea8b4` — allow `schedule` points in cloud/edge bulk reads.
- `31139da` — make long Recent Jobs errors left-aligned, wrapped, and scrollable.
- `459fd7e` — group legacy updater phases into UI Update and Provision + Agent Update.

The edge UI repository `ssmithiot/edge-bacnet-commissioning-ui` is at:

- `51a80c0` — readable Live Devices View action.
- `97c7258` — restored BACnet runtime-mode selector (47809/47814/dual profiles).
- `8ae4e0a` — fixed missing Live Devices export route that caused `/devices` HTTP 500.

The legacy updater is now the operational deployment tool:

- `tools/legacy_edge_upgrade_webapp.py`
- Defaults to all phases selected and runs selected phases consecutively.
- Supports Select all/Clear all and grouped UI-only vs agent/provisioning phases.
- UI-only redeploys should select inspection, backup, upload, apply, auth, and UI restart.
- Agent/cloud redeploys should select inspection, provisioning, repo update, config/token, agent install, service restart, and final verification.

Important current issue:

- GW044’s original cloud failure was not an RPM/network failure. Point 67 was rejected because its object type was `schedule`, which was not in the original bulk-read allowlist. `schedule` support is now live in production. Retry GW044 after Render finishes deploying `51ea8b4` and after its edge agent is updated from `main`.
- The improved validation error now includes the received object type for future diagnosis.

Remaining work for tomorrow:

1. Confirm GW044 cloud bulk read succeeds for all 68 points, including the schedule point.
2. Add the full BACnet property registry and selected-property reads (relinquish-default 104, priority-array 87, units 117, description 28, status-flags 111, reliability 103, out-of-service 81).
3. Support custom tables containing points from multiple devices/controllers; group cloud jobs by `device_instance`.
4. Add refresh performance UI: points updated, elapsed time, RPM blocks, fallback count, source, and failures.
5. Begin trends design and implementation.
6. Perform project housekeeping after live behavior is stable: reconcile untracked bundles/snapshots, document release artifacts, and clean branch history deliberately.

Deployment vocabulary:

- “Deploy/make live” means merge or push to production `main` and allow Render to deploy.
- “Test branch” means keep work separate from production.
- “Local test” means no push or deployment.

Pushed to `main`:

- `a722300 Batch point value reads`
  - Cloud queues `bacnet_read_bulk` jobs grouped by `device_instance`.
  - Cloud persists returned `values[]` into saved points.
  - Edge agent learned `bacnet_read_bulk`.
- `df120e7 Improve point table refresh feedback`
  - Moved read status near `Refresh values`.
  - Pinned `Present Value` next to `Name`.
- `de4c7cb Show point job failure details`
  - Recent Jobs shows failure details when `error_message` or result error exists.
  - Fixed light-mode table text visibility.
- `913d4f6 Fix dark tree label contrast`
  - Folder/device/point labels readable in dark mode.
- `4566b47 Complete bulk reads with edge fallback`
  - Edge still uses one cloud job.
  - Edge attempts RPM bulk reads.
  - If RPM block output does not parse point values, edge falls back to single `bacrp` reads inside the same job.
  - Edge posts one combined `values[]` result.
  - Result includes `read_source` per point and `single_read_fallback_count`.

## Deployable Edge Update Bundle

Created local field update artifact:

`C:\Users\steph\OneDrive\Documents\iot-cloud-commissioning\edge-agent-bulk-read-update-20260710.zip`

SHA256:

`B56552D063E3F6200D4D4661A2F6E2C629FC25E3C3E81AAC2811621C3194E9CF`

Zip contents:

- `install.sh`
- `README.md`
- `MANIFEST.txt`
- `edge-agent/iot_cx_agent/bacnet.py`
- `edge-agent/iot_cx_agent/jobs.py`
- `edge-agent/iot_cx_agent/config.py`
- `edge-agent/pyproject.toml`
- `edge-agent/requirements.txt`
- `deploy/iot-cx-agent.service`

Install on a gateway:

```bash
cd /tmp
unzip edge-agent-bulk-read-update-20260710.zip -d edge-agent-bulk-read-update-20260710
cd edge-agent-bulk-read-update-20260710
chmod +x install.sh
./install.sh
```

If the repo is not at `/home/swadmin/iot-cloud-commissioning`:

```bash
REPO_ROOT=/path/to/iot-cloud-commissioning ./install.sh
```

The installer backs up current edge-agent files, copies the updated files into the gateway repo, reinstalls the editable Python package, reinstalls the service unit, restarts `iot-cx-agent.service`, and prints recent logs/status.

## What We Observed

Cloud table:

- The point table now shows `Name`, `Present Value`, `Object Identifier`, and actions.
- `Path` was removed from the table.
- Present values now show from persisted saved-point values while waiting for a new refresh.
- The warning/status message now appears near `Refresh values`.

Edge direct UI:

- Gateway direct UI handled 32 points in under a minute.
- It reports `Read method: RPM`.
- It also showed `RPM properties parsed: 0` and `Single-read fallbacks: 69`.
- This strongly suggests the local UI completes successfully because it falls back to single reads when RPM block parsing does not return values.
- Cloud edge-agent bulk path was updated to match that practical behavior: try RPM, fall back per point inside the same cloud job, post one combined result.

Recent cloud behavior:

- `bacnet_read_bulk` job failed without enough visible detail before the latest UI change.
- Recent Jobs should now show `error_message` / result error details after Render deploy.
- If a job stays `queued`, the gateway agent is not claiming it.
- If it becomes `failed`, inspect `error_message` and `result_json`.

## Important Distinction

`bacrpm` exists on all gateways and works directly. That does not automatically mean the background `iot-cx-agent` service has the new `bacnet_read_bulk` fallback code.

The cloud deploy updates the web/API server. Existing gateways need their edge-agent code updated/restarted using the bundle above or the normal repo update script.

## Verification Commands

On a gateway after installing the bundle:

```bash
systemctl is-active iot-cx-agent.service
journalctl -u iot-cx-agent.service -n 80 --no-pager -l
```

From cloud/API:

```text
https://iot-cloud-api-dev.onrender.com/api/edge/jobs?limit=20
```

Look for:

- `job_type = bacnet_read_bulk`
- `status = completed`
- `result_json.values`
- `result_json.value_count`
- `result_json.single_read_fallback_count`
- per-point `read_source`

## Tests Already Run

Ran successfully:

```bash
python -m py_compile edge-agent\iot_cx_agent\bacnet.py edge-agent\iot_cx_agent\jobs.py
python -m pytest edge-agent\tests\test_bacnet_read.py -k "bulk or success"
```

Result:

- `4 passed`

Also compile-checked UI patches:

```bash
python -m py_compile cloud-api\app\ui.py
```

## Dirty Worktree Note

There are unrelated dirty/untracked files in the repo from earlier GW025 / gateway-update work. Do not blindly commit everything.

Known unrelated dirty areas include:

- `README.md`
- `docs/field-gateway-update.md`
- `edge-agent/config.example.yaml`
- `edge-agent/iot_cx_agent/status.py`
- `edge-agent/tests/test_bacnet.py`
- several `scripts/*`
- `.gw025-live*`
- `gw025-bacrtr-update-*`
- `tools/`

The edge-agent bulk update bundle is also untracked:

- `edge-agent-bulk-read-update-20260710/`
- `edge-agent-bulk-read-update-20260710.zip`

Leave these alone unless explicitly deciding what to archive/commit.

## Recommended Next Steps Tomorrow

1. Wait for Render deploys from latest pushes to finish.
2. Hard refresh the cloud UI.
3. Install `edge-agent-bulk-read-update-20260710.zip` on one test gateway, ideally `GW036` or the gateway used for the point table test.
4. Restart `iot-cx-agent.service`.
5. Clear any stale queued/failed `bacnet_read_bulk` jobs if needed.
6. Trigger `Refresh values` from the cloud table.
7. Check Recent Jobs and `/api/edge/jobs?limit=20`.
8. Confirm one `bacnet_read_bulk` job completes with `values[]`.
9. Confirm table values update and persist.
10. If successful, roll the update bundle to additional gateways.

## SQL To Clear Stale Queued Bulk Jobs

All queued bulk jobs:

```sql
update edge_jobs
set
  status = 'failed',
  error_message = 'Manually cleared stuck queued bacnet_read_bulk job',
  completed_at = now()
where status = 'queued'
  and job_type = 'bacnet_read_bulk';
```

Only one gateway:

```sql
update edge_jobs
set
  status = 'failed',
  error_message = 'Manually cleared stuck queued bacnet_read_bulk job',
  completed_at = now()
where status = 'queued'
  and job_type = 'bacnet_read_bulk'
  and gateway_id = 'GW036';
```
