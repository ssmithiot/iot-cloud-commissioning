# Staging First-Deploy Verification Worksheet

Fill this in during the first staging deploy. Commands are bash-style (Git Bash/WSL); on PowerShell use `curl.exe` and adjust quoting. Set once:

```bash
export STG="https://<staging-service>.onrender.com"   # staging URL — never production
export ADMIN="<staging-admin-api-token>"              # staging IOT_ADMIN_API_TOKEN
```

## Record of environment

| Field | Value |
|---|---|
| Staging Render URL | |
| Staging Render service name | |
| Staging Supabase project name | |
| Staging database host (pooler) | |
| Deploy date / git ref deployed | |
| Operator performing validation | |

## 1. Render health + environment identity

```bash
curl -s $STG/health
```
Expect exactly: `{"status":"ok","environment":"staging","version":"0.1.0"}`.
- [ ] `environment` is `staging` (if it says `development`, `ENVIRONMENT` is unset; if `production`, stop immediately)
- [ ] Startup succeeded (if the service crash-loops with "configured with known production resources", a fingerprint matched — fix the env vars; do not set `ALLOW_PRODUCTION_RESOURCES=true`)

Recorded environment value: ______

## 2. Database connectivity and migration head

```bash
curl -s $STG/health/db
curl -s $STG/health/schema
```
- [ ] `/health/db` → `{"status":"ok"}`
- [ ] `/health/schema` → `status":"ok"` and `expected_revisions` == `current_revisions` == `["0017_gateway_alert_states"]` (the Alembic head of the deployed ref)

Recorded migration head: ______

## 3. Authentication (and cross-environment rejection)

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ADMIN" $STG/api/ui/gateways      # expect 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer wrong-token" $STG/api/ui/gateways  # expect 401
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ADMIN" https://iot-cloud-api-dev.onrender.com/api/ui/gateways  # expect 401 — staging token MUST NOT work on production
```
- [ ] 200 / 401 / 401 as noted. If the third returns 200 you copied the production token — rotate it and start over.

Browser: sign up at `$STG/signup` with a synthetic user (e.g. `staging-op@example.test` via the staging Supabase project), then approve at `$STG/admin/users` using the admin token.
- [ ] Synthetic operator created and approved. Recorded user: ______

## 4. Synthetic gateway creation

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"gateway_id":"STG-GW001","site_id":"staging-site-a","hostname":"stg-gw001"}' \
  $STG/api/admin/gateways/provision
```
- [ ] Response includes `gateway_api_token` — record it as `$GWTOK` below. It is a staging-only secret.

```bash
export GWTOK="<gateway_api_token from above>"
```
Recorded gateway ID: STG-GW001 · site: staging-site-a

## 5. Synthetic heartbeat

```bash
curl -s -X POST -H "Authorization: Bearer $GWTOK" -H "Content-Type: application/json" -d '{
  "gateway_id":"STG-GW001","site_id":"staging-site-a","hostname":"stg-gw001",
  "lan_ip":"10.0.0.1","bacnet_port":47814,"agent_version":"0.0.0-staging","ui_version":"0.0.0-staging",
  "sqlite_db_ok":true,"queued_upload_count":0,
  "timestamp_utc":"'"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"'"}' $STG/api/edge/heartbeat
```
- [ ] Returns `{"gateway_id":"STG-GW001","status":"online",...}`; gateway shows online in `$STG/app`.

## 6. Job polling round-trip

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"gateway_id":"STG-GW001","job_type":"echo","request":{"ping":1}}' $STG/api/edge/jobs
curl -s -H "Authorization: Bearer $GWTOK" $STG/api/edge/STG-GW001/jobs/next   # expect the echo job, claimed
curl -s -H "Authorization: Bearer $GWTOK" $STG/api/edge/STG-GW001/jobs/next   # expect null (no double claim)
```
- [ ] Job claimed exactly once. Recorded job_id: ______

## 7. Trend ingestion and retrieval

```bash
# 7a. Create a synthetic device and point (capture the returned ids)
curl -s -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"device_instance":9001,"device_name":"Staging AHU"}' $STG/api/ui/gateways/STG-GW001/devices
export DEVICE_ID="<id from response>"
curl -s -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"object_type":"analog-input","object_instance":1,"object_name":"Zone Temp"}' \
  $STG/api/ui/devices/$DEVICE_ID/points
export POINT_ID="<id from response>"

# 7b. Enable a trend config
curl -s -X PUT -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"enabled":true,"interval_sec":300}' $STG/api/ui/points/$POINT_ID/trend

# 7c. Upload one batch as the gateway, twice (second must not duplicate)
curl -s -X POST -H "Authorization: Bearer $GWTOK" -H "Content-Type: application/json" \
  -d '[{"point_id":"'$POINT_ID'","sampled_at":"2026-07-12T12:00:00+00:00","value":"21.5","quality":"good"}]' \
  $STG/api/edge/STG-GW001/trend-samples
# repeat the same command — expect the same sample back, no error, no duplicate

# 7d. Retrieve
curl -s -H "Authorization: Bearer $ADMIN" "$STG/api/ui/points/$POINT_ID/trend?limit=10"
```
- [ ] Upload accepted; retry idempotent; retrieval shows the sample with `quality`, `source`, `received_at`.

## 8. Tenant isolation

```bash
# Second site + gateway
curl -s -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"gateway_id":"STG-GW002","site_id":"staging-site-b","hostname":"stg-gw002"}' \
  $STG/api/admin/gateways/provision
# Scope the synthetic operator to site A only (site UUID from /api/ui/sites)
curl -s -H "Authorization: Bearer $ADMIN" $STG/api/ui/sites
curl -s -X PUT -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"email":"staging-op@example.test","role":"operator"}' \
  $STG/api/admin/sites/<staging-site-a-uuid>/members
```
Then, in the browser logged in as the synthetic operator:
- [ ] Gateway list shows STG-GW001 only (STG-GW002 absent)
- [ ] Direct URL `$STG/api/ui/gateways/STG-GW002` returns 404 for the operator's token
- [ ] `$STG/api/edge/jobs` created for STG-GW002 by the operator returns 404

## 9. No tunnel connected

```bash
curl -s -H "Authorization: Bearer $ADMIN" $STG/api/ui/gateways/STG-GW001/tunnel-status
```
- [ ] `connected` is `false`; no production gateway or tunnel points at staging (review production gateway configs by inventory, not by staging data).

## Result

| Field | Value |
|---|---|
| Checklist result (pass/fail per section) | 1:__ 2:__ 3:__ 4:__ 5:__ 6:__ 7:__ 8:__ 9:__ |
| Evidence (links/screenshots) | |
| Anomalies observed | |
| Rollback notes (what to delete/disable if abandoning) | Suspend the Render service; delete staging Supabase project or its data; revoke staging admin token; no production impact possible if isolation held. |

Next after all sections pass: `docs/staging-trend-validation-checklist.md`, then load Levels 1–3 per `docs/cloud-platform-staging-load-test-plan.md`.
