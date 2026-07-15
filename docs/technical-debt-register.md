# Technical Debt Register

Prioritized engineering backlog. Sources: Phase 1/2 reports, scaling assessment, architecture review (2026-07), fleet readiness review. Every item cites evidence. Re-triage at each phase boundary; move items up only with evidence.

## Tier 1 — Must complete before rollout expansion (160 sites go-live)

| # | Item | Evidence / why now | Size |
|---|---|---|---|
| 1 | ~~Offline-gateway alerting~~ **Groundwork implemented 2026-07-13** (transition-based offline + trend-backlog alerts, `POST /api/admin/alerts/evaluate`, Slack-compatible webhook, silent until `ALERT_WEBHOOK_URL` set; migration 0017; 7 tests). Remaining: schedule Render cron + set webhook URL when the fleet build-out makes it useful — pending review | S–M |
| 2 | ~~Credential revoke endpoint + credential listing~~ **Implemented 2026-07-13** (`GET /api/admin/gateways/{id}/credentials`, `POST /api/admin/credentials/{id}/revoke`; 6 tests) — pending review | S |
| 3 | ~~Stale-job reaper (poll-time, `claimed_at` timeout)~~ **Implemented 2026-07-13** (`JOB_CLAIM_TIMEOUT_SEC`, requeue at poll, write-batch jobs excluded; 4 tests) — pending review | S |
| 4 | ~~CI pipeline running all suites~~ **Implemented 2026-07-13** (`.github/workflows/ci.yml`: cloud+edge+tools suites, migration round-trip, compileall on py3.11; activates on first push to GitHub). Also fixed en route: 6 Linux exec-bit stub failures, 2 stale lock-format fixtures, 1 obsolete 47808-rejection assertion, 2 module-import-order env flakes, 1 Alembic-logging test interaction — the full suite is now green on Linux and order-independent — pending review | S |
| 5 | `ENVIRONMENT=production` set on prod; Dockerfile self-migration removed (pre-deploy = sole authority) | Identity feature shipped; Dockerfile CMD still migrates at container start → race if instances ever >1 [Dockerfile] | XS (deploy-coordinated) |
| 6 | Staging validated end-to-end (worksheet, trend checklist, load L1–2) | All docs/harness ready; nothing measured yet [Phase 2/staging prep] | Operator time |
| 7 | ~~Fleet update batch driver with post-update health gate~~ **Re-scoped and implemented 2026-07-13.** Batch queuing + automated worker already existed (UI multi-select → `GatewayUpdateRequest` queue → upgrade-webapp worker). Added: cloud-observed post-update health gate (fresh heartbeat + online + sqlite ok before an update counts as complete; `IOT_EDGE_UPDATE_HEALTH_*` env controls) and stop-the-line (worker halts after `IOT_EDGE_UPDATE_HALT_AFTER_FAILURES` consecutive failures, default 2; restart to resume). 8 tests — pending review. Remaining for Tier 3 (#26): routine-update light mode, artifact signing, auto-rollback | M→S |
| 8 | Audit events for sensitive actions (provision, role change, credential ops, admin-token use) | `security-model.md` already specifies; incident response is blind without it | S–M |

## Tier 2 — Must complete before customer #2 (multi-tenant)

| # | Item | Evidence | Size |
|---|---|---|---|
| 9 | Retire zero-membership full-access fallback (backfill memberships, flag-gated cutover) | `access.py` legacy fallback = tenant isolation is opt-in [arch review risk #1]. **Slice 1 implemented 2026-07-14** on branch `customer2-tenancy-slice1`: `REQUIRE_EXPLICIT_MEMBERSHIP` flag added (default `false`, no behavior change); when `true`, a zero-organization/zero-site operator or viewer gets an empty allowed-site set instead of global visibility; platform admins (`role=="admin"`) and the admin token are unaffected either way. 9 tests (`test_site_access.py`, `test_tenancy_isolation.py`) cover both flag states, scoped-operator non-regression, cross-org guessed-ID isolation, and admin/token retained access. **Not yet done:** Customer 1 membership backfill (prerequisite — flipping the flag before backfill would lock out every pre-existing operator with no assigned membership), org/customer-admin self-service capabilities, and flipping the flag itself in any environment. See `docs/staging-environment-variables.md` for the flag and rollout order. | M (needs approval — behavior change) |
| 10 | Per-resource tenant boundary tests (trends, credentials, templates, tunnel sessions, write batches) | Scoping helpers verified at call sites; per-resource tests partial [Phase 2 §4] | S–M |
| 11 | Org onboarding/invitation flow (create org, invite member, assign sites without admin-token surgery) | Admin endpoints exist; workflow doesn't [main.py admin routes] | M |
| 12 | Login rate limiting + token expiry defaults for new gateway credentials | No rate limiting anywhere; credentials non-expiring unless set [auth.py] | S–M |
| 13 | Duplicate job-result terminal-state guard (409 after completion) | Last-write-wins overwrite [receive_job_result]; verify edge retry semantics first | S |
| 14 | Fleet dashboard v1 (info model documented in fleet-operations-readiness.md Part B) | Data exists; view doesn't | M |
| 15 | Diagnostics-bundle job type (edge returns logs/queue stats on request) | SSH-only diagnostics today [field guide §9] | M |
| 16 | Error aggregation (Sentry-class) | Logs only; no exception visibility between deploys | S |

## Tier 3 — Must complete before enterprise scale (~1,300 sites / multi-instance)

| # | Item | Evidence | Size |
|---|---|---|---|
| 17 | Tunnel service extraction | Process-local registries block >1 worker/instance [tunnel.py; arch review risk #2] | L |
| 18 | Portal frontend (Vercel) replacing `ui.py`; freeze `ui.py` now | 7,241-line inline UI [arch review risk #3] | L (phased) |
| 19 | Typed trend values + units pipeline (dual-write migration) | `value String(255)` blocks aggregation/analytics [models.py] | M |
| 20 | Trend partitioning + retention/aggregation background worker (first separate service) | Phase 3 by design; retention currently piggybacks on request paths | L |
| 21 | Pagination on fleet listing endpoints (+ UI) | `/api/ui/gateways` unbounded [main.py]; fine at 160, not at 1,300 | S–M |
| 22 | Shared-backend rate limiting (per-gateway) | Staged design documented; needs Redis-class infra only at fleet scale [assessment §6] | M |
| 23 | Pepper/token rotation workflow (dual-pepper verification window) | Pepper rotation currently bricks fleet auth [DR runbook §4] | M |
| 24 | `main.py` split by router; changed-fields-only heartbeat update; `ui_list_gateway_updates` N+1 fix | Monolith + write amplification + N+1 [Phase 2 findings] | M |
| 25 | Metrics backend (OpenTelemetry path documented) | Logs-only observability ceiling [assessment §12] | M |
| 26 | Staged edge rollout with automatic health-gated rollback + artifact signing | Manual canary discipline only [release process] | L |
| 27 | Desired-state gateway configuration with drift reporting | Config invisible to cloud [fleet review] | L (design first) |

## Tier 4 — Can safely wait (re-evaluate on evidence)

| Item | Why deferrable |
|---|---|
| Semantic equipment model + tagging (pre-AI) | Prerequisite for Phase 5, not for operations; design during Tier 3 era [arch review §10/§12] |
| Alarm engine | Commercial table stakes, but not needed by the commissioning-led rollout; schedule with semantic model |
| AI analytics | Deliberately year-two [arch review §14] |
| Modern portal *completion* (beyond top workflows) | Phased under #18 |
| `supabase/migrations/` archival | Governance doc already neutralizes it [SCHEMA_GOVERNANCE.md] |
| Weather fetch async conversion; idempotency SELECT batching; request compression | No measured pain [assessment §3] |
| Windows/Linux test-stub friction (`test_bacnet.py`, `tomllib`) | Cosmetic; document in CI matrix instead |
| Pre-existing `test_gateway_workspace_contains_discovery_progress_ui` failure | Fix opportunistically with the next workspace-UI change (assert against current UI or restore the button intentionally) |

## Never worth fixing (recorded to stop re-litigating)

- Replacing the Postgres job queue with a broker at current contention [arch review §9-tech-debt].
- Rewriting the edge sequential loop as async/multi-worker — its simplicity is the reliability feature.
- Replacing CLI BACnet wrappers before COV/alarms force a native stack — field-proven, well-abstracted.
