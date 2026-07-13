# Fleet Operations Readiness — 160 Gateways

Perspective: one operator responsible for ~160 active gateways today, ~1,300 in 3–5 years. Facts cite repository evidence; nothing here is implemented by this document.

## Part A — Operational burden review (what 160 gateways feels like today)

| Task | Today | Burden at 160 | Verdict |
|---|---|---|---|
| Provisioning | One API call + one on-gateway script per unit; clone-safe master image [FACT — scripts/] | ~10 min/unit hands-on, front-loadable | **Acceptable** for the rollout; bulk CSV provisioning becomes worthwhile at the retrofit program |
| Updates | Per-gateway SSH script, sequential [FACT — update-edge-agent.sh] | 160 × ~3 min per release + attention; no health gate | **Highest burden.** Batch driver + post-update health verification is the #1 tooling need |
| Rollback | Same script with previous ref; manual per gateway | Manageable only because updates are manual/batched | Same fix as updates |
| Version tracking | agent/ui versions in every heartbeat, visible per-gateway [FACT — EdgeNode] | No fleet roll-up; answering "who is not on v0.1.6" = eyeballing a list | **Cheap win:** one fleet version-distribution query/endpoint |
| Diagnostics | SSH + journalctl + SQLite spelunking; field guide documents it | ~30 min/incident, requires SSH-capable person | Diagnostics bundle (edge job that returns logs/queue stats) is the #2 tooling need |
| Recovery | Field guide procedures, all manual | OK at current failure rates [INFERENCE] | Acceptable; revisit with real MTBF data |
| Credential management | Provision issues tokens; **no revoke API** — rotation ends in manual DB row edit [FACT — main.py provision; no revoke route] | Every rotation/replacement touches engineering | **Must fix before rollout expansion:** revoke endpoint + list-credentials view |
| Offline detection | Status computed on page view; **nobody is notified** [FACT — no alerting anywhere] | Operator must habitually look | **Must fix:** offline-transition alert (email/webhook) — the single highest-value item on this page |
| Config drift | Edge config is a local file; cloud never sees it [FACT] | Invisible drift (cloud_url, intervals, tool paths) | Report config hash/snapshot in heartbeat (additive) → drift list; desired-state config is the long-term shape |
| Trend backlog watch | Per-gateway backlog fields in heartbeat + UI [FACT — Phase 1] | Good data, no aggregation/threshold alarm | Fold into alerting + dashboard |

**Priority order for future implementation (do not build now):** 1) offline alerting, 2) update batch driver with health gate, 3) credential revoke/list tooling, 4) fleet version/backlog roll-up endpoint, 5) diagnostics-bundle job type, 6) config snapshot/drift report. Items 1, 3, 4 are small; 2 and 5 are medium; 6 is a design exercise first.

## Part B — Operations dashboard information model (define now, build later)

Principle: every element below already exists in the database or is a trivial aggregate of it — the dashboard is a view, not new plumbing. [FACT — all fields cited exist in `EdgeNode`, `EdgeHeartbeat`, `EdgeJob`, `GatewayUpdateRequest`, `GatewayCredential`, `PointTrendSample`]

### Fleet overview (landing view)
- Gateway counts by effective status (online / stale / offline) — exists as `/api/ui/gateways/summary`
- Offline list sorted by offline-duration (from `latest_heartbeat_at`)
- Trend backlog outliers: top N by `trend_pending_upload_count`, any with `trend_oldest_pending_at` older than X hours
- Version distribution: count by `agent_version` (and ui_version); highlight below-minimum versions
- Job failures last 24 h: count + list from `EdgeJob.status='failed'` with error_message
- Deployment status: open `GatewayUpdateRequest` rows by status (queued/running/failed)

### Per-gateway drill-down (mostly exists in today's UI)
- Identity: gateway_id, site, hostname, LAN IP, BACnet port
- Health: effective status, heartbeat age, sqlite_db_ok, CPU/memory/disk trend (from `EdgeHeartbeat` history — endpoint exists)
- Trend health: pending/deferred counts, oldest pending, max attempt count (exists)
- Versions: agent/ui + last change time
- Jobs: recent jobs with status/durations (claimed_at→completed_at)
- Tunnel: connected yes/no (exists); session history = **gap**, no session records persist [FACT — process-local]
- Credentials: prefix, created, last_used_at, revoked/expiry — **gap:** no listing endpoint

### Support / diagnostics view (future)
- Diagnostic summaries per incident (needs the diagnostics-bundle job type — gap)
- Heartbeat attempt journal from the edge (exists on-gateway in SQLite; not uploaded — gap, upload-on-request is enough)

### Data the dashboard must NOT show
- Raw tokens (never stored), token hashes, pepper, admin token, database URLs — consistent with `/health` policy.

### Alert conditions worth defining now (thresholds tunable)
- Gateway offline > 2× heartbeat interval → warn; > 1 h → alert
- `trend_oldest_pending_at` > 6 h → warn (edge can't drain)
- `sqlite_db_ok=false` on any heartbeat → alert
- Job failed count for one gateway > 3 in 1 h → warn
- Fleet-wide: >5% offline simultaneously → major (cloud-side incident likely)
