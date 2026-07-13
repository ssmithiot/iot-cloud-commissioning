# Architecture Review — IoT Cloud Commissioning Platform

**Review date:** 2026-07-12 · **Reviewer role:** Principal Software Architect · **Candidate release:** 0.1 (branch `codex/trend-hardening`, local working tree)
**Method:** source-only review of cloud API, edge agent, migrations, tests, deployment scripts, and all documentation, including the 2026-07-11 Foundation & AI-Readiness Assessment and the Phase 1 (trend hardening) and Phase 2 (scaling foundations) work completed this cycle. No code changed by this review.

Every claim is labeled **[FACT]** (repository evidence), **[INFERENCE]** (reasoned from evidence, not measured), or **[RECOMMENDATION]**.

---

## 1. Executive Summary

**Can this become a commercial Edge-to-Cloud BAS platform? Yes — conditionally.** The load-bearing architectural decisions are correct and hard to get right retroactively: BACnet execution stays on the edge; the edge speaks outbound-only HTTPS/WebSocket; the cloud is a single Postgres system of record with no background workers; gateways buffer locally and survive cloud outages; the schema is migration-governed with startup verification. [FACT — `docs/architecture.md`, `edge-agent/iot_cx_agent/`, `app/schema.py`, `docs/architecture/SCHEMA_GOVERNANCE.md`] These are the decisions that sink platforms when wrong, and they are right here.

What stands between Release 0.1 and a commercial product is not architecture but breadth and hardening: tenant isolation is opt-in rather than default [FACT — `access.py` zero-membership fallback]; the tunnel pins the platform to one process [FACT — `tunnel.py` module-level dicts]; the browser UI is 7,241 lines of Python-embedded HTML/JS that will not survive a commercial UX bar [FACT — `ui.py`]; there are no alarms/events, no audit log, no CI pipeline, no staging environment yet (preparation complete this cycle), and no measured performance envelope. [FACT]

**[INFERENCE]** With 12 disciplined months focused on tenancy, fleet operations, a real frontend, and the typed trend engine — in that order — this codebase is a viable commercial foundation. Its greatest asset is restraint: no premature microservices, no unneeded brokers, an unusually strong test and documentation culture. Its greatest liability is that several conveniences appropriate for a single-operator system (shared admin token, full-access fallback, inline UI) are structurally embedded in the paths customers would touch first.

---

## 2. Overall Architecture Grade

| Area | Score | Justification |
|---|---:|---|
| Cloud architecture | **7** | [FACT] Clean layering (auth → access → routes → models), zero background work, DB-backed caches, stateless except the tunnel. Deductions: `main.py` at ~3,200 lines is a single-module monolith; tunnel state is process-local; weather fetch is a blocking call in-request. |
| Edge architecture | **7.5** | [FACT] Sequential single-process loop with SQLite store-and-forward, bounded backlog, exponential retry, heartbeat attempt journal, systemd restart. Simple, debuggable, offline-tolerant. Deduction: one loop means a slow job delays trend sampling [INFERENCE from `main.py` run_once ordering]. |
| BACnet architecture | **5.5** | [FACT] Discovery, object-list load, single/bulk reads, priority-array-aware writes with staged cloud approval, via `bacwi/bacrp/bacrpm` CLI wrappers with a per-port lock. No COV, no alarm/event ingestion, no BACnet trend-log reads, no native protocol stack; per-port lock serializes all BACnet work [INFERENCE: throughput ceiling per gateway]. Correct boundary, limited depth. |
| Database design | **7** | [FACT] Coherent SQLAlchemy 2.x models, guarded/reversible Alembic migrations (0001–0016), startup schema verification, hot-path composite indexes (Phase 2). Deductions: trend values are strings; job payloads are opaque JSON; the parallel `supabase/migrations/` lineage invites drift (governance doc exists but the tree remains). |
| Security | **6.5** | [FACT] Peppered-HMAC gateway tokens with one-time issuance, prefix lookup, revocation/expiry columns; Supabase JWT verification (HS256+JWKS); role model; centralized site scoping with 404 anti-enumeration; BACnet writes gated by staged approval. Deductions: shared admin token in routine use, zero-membership full-access fallback, no audit log, no rate limiting, manual rotation only. |
| Scalability | **6** | [FACT] Phase 2 added pool controls, locked job claiming, auth-write throttling, retention, and indexes; load model documented. Deductions: single worker/instance ceiling unmeasured; tunnel blocks horizontal scale; no pagination on fleet listings. |
| Maintainability | **5.5** | [FACT] `ui.py` = 7,241 lines of HTML/JS in Python strings (12 defs); `main.py` = 3,214 lines, 76 routes. Strong tests and docs offset this, but both files are change-amplifiers [INFERENCE: merge conflicts and regression risk grow with team size]. |
| Testability | **8** | [FACT] ~260 fast tests across cloud/edge/tools; SQLite-backed API tests with dependency overrides; schema-governance tests; migration round-trips verified. Weakness: UI tested by string assertion only; no browser-level tests. |
| Deployment model | **5** | [FACT] One Render service, Docker CMD couples migration to container start (race if >1 instance), no CI pipeline in repo, no IaC, staging not yet created (prep docs ready). |
| Operational readiness | **4.5** | [FACT] Gateway update queue + SSH scripts + legacy upgrade webapp exist; heartbeat telemetry is good. No alerting, no health dashboards beyond the app UI, no diagnostics bundles, no staged rollout/rollback for edge. |
| Observability | **5.5** | [FACT] Structured request-timing logs (Phase 2), rich heartbeat/backlog telemetry, `/health*` triad with schema status. No metrics backend, no tracing, no error aggregation, no alerting hooks. |
| Documentation | **8** | [FACT] Exceptional for project size: architecture, security model, schema governance, ERD, scaling assessment, failure/recovery, staging suite, load-test plan, phase reports. Deduction: known drift between older docs and code (self-identified in the Foundation Assessment). |
| Developer experience | **6.5** | [FACT] Fast tests, clear charters, disciplined phase reports. Deductions: monolith files, no CI to enforce the discipline, Windows/Linux test friction (exec-bit, tomllib). |

**Composite: ~6.3/10 — a strong foundation with deliberate, well-documented gaps.** [INFERENCE]

---

## 3. Biggest Strengths

1. **BACnet stays on the edge, absolutely.** [FACT] The cloud requests work via jobs; only the gateway touches UDP. This is the boundary that makes the product deployable behind customer firewalls without network re-engineering, and it is enforced in code, tests, and charter.
2. **Outbound-only connectivity.** [FACT — heartbeat, polling, uploads, and the tunnel are all edge-initiated.] No inbound ports, no VPN, no per-site IT negotiation. This is the single largest commercial deployment advantage a BAS cloud can have.
3. **Store-and-forward edge with bounded backlog.** [FACT — `db.py` sync_queue, backlog cap, retry metadata, attempt tracking.] Cloud outages lose nothing within bounds; recovery is automatic; backlog health is *reported upstream* in heartbeats — the edge tells you it is suffering.
4. **Gateway credential design.** [FACT — `iotcc_gw_` prefix + peppered HMAC hash, raw token shown once, revocation/expiry columns, constant-time compare.] A stolen database does not yield usable gateway tokens; a stolen pepper alone doesn't either.
5. **One system of record, zero background workers.** [FACT] All periodicity is driven by edges and browsers; every cloud instance is stateless except the tunnel. The failure model fits on one page (`cloud-platform-failure-recovery.md`) *because* of this decision.
6. **Schema governance with runtime enforcement.** [FACT — `require_current_schema` fails startup on drift; `/health/schema` exposes it; migrations are guarded and reversible; a governance doc names Alembic as sole authority.] Most projects this size have nothing comparable.
7. **Idempotent, quality-aware trend ingestion.** [FACT — Phase 1: (point, sampled_at) uniqueness, batch bounds, duplicate rejection, quality/source/received_at, retention.] The ingestion contract is retry-safe end to end, which is precisely what fleet-scale telemetry needs.
8. **Staged BACnet write approval.** [FACT — write batches are created pending approval, approved explicitly, executed on the edge, and audited (`0014_bacnet_write_audit`).] Writing to a live building is the highest-consequence action this product takes; it is the best-guarded path in the system.
9. **Centralized authorization helpers.** [FACT — `visible_site_ids`/`require_site_access` + `_require_*_site_access` used across UI routes, 404 on scope miss.] One place to reason about tenancy; Phase 2 extended it to the legacy endpoints.
10. **Right-sized job queue.** [FACT — Postgres-backed jobs with per-gateway serial pollers, now row-locked claiming.] No broker to operate, exactly matching the contention profile; the threshold to revisit is documented rather than guessed.

---

## 4. Biggest Weaknesses (ranked by severity)

| # | Risk | Impact | Probability | Recommended solution | Urgency |
|---|---|---|---|---|---|
| 1 | **Tenant isolation is opt-in** — zero-membership active operators see all sites [FACT — `access.py` fallback] | Cross-customer data exposure the day a second customer exists | Certain under multi-tenant onboarding without process discipline | Membership backfill + retire fallback behind a flag; per-resource boundary tests (Phase 2C spec exists) | **Before first commercial tenant** |
| 2 | **Tunnel pins platform to one process** [FACT — module-level registries] | Hard ceiling on availability and horizontal scale; a deploy drops all consoles | Certain once >1 worker/instance is needed | Extract tunnel service (Stage D boundary documented); until then keep single instance deliberately | Before any multi-instance move |
| 3 | **`ui.py` inline-HTML monolith** [FACT — 7,241 lines, 12 functions] | Feature velocity collapse; XSS surface concentrated in hand-built string templating; unreviewable diffs | High as UI expectations grow | Adopt the already-planned Vercel/portal path (`docs/architecture.md`); freeze `ui.py` feature growth; move new UI to the portal | Before Phase 4 (visualization) |
| 4 | **No stale-job recovery** [FACT — no reaper; `claimed` is forever if a gateway dies mid-job] | Stuck work, misleading UI, operator confusion at fleet scale | Medium-high at 100+ gateways | Poll-time reaping with `claimed_at` timeout (Phase 2B spec exists) | Next implementation cycle |
| 5 | **Shared admin token as routine credential** [FACT — used by scripts, smoke tests, worksheets] | Single secret = full platform control; no attribution, no rotation story | Medium; grows with team/customer count | Per-operator admin roles already exist — demote the token to break-glass; add audit events for its use | Before 50 customers |
| 6 | **No audit log** [FACT — only BACnet writes are audited] | Cannot answer "who did what" for role changes, provisioning, credential access; blocks enterprise sales and incident response | Certain to be required | `audit_events` table (already envisioned in `security-model.md`) + writes at the ~10 sensitive call sites | Before enterprise deals; cheap now |
| 7 | **String-typed trend values** [FACT — `PointTrendSample.value String(255)`] | Every analytics/AI feature pays a parsing tax; aggregation in SQL impossible cleanly | Certain by Phase 3/5 | Additive typed columns (`value_num`, units on config/point) with dual-write migration window | Design now, execute early Phase 3 |
| 8 | **Unmeasured performance envelope** [FACT — no staging, no load data; Phase 2 numbers labeled PROJECTED] | Capacity planning is guesswork; risk of discovering ceilings in production | Medium | Execute staging Levels 1–3 (plan + harness ready) | Immediately after staging exists |
| 9 | **No rate limiting or abuse protection** [FACT — design doc only] | Retry storms or a leaked credential can saturate the single instance | Medium | DB-backed counters for login/provision/tunnel-create first; defer per-gateway limits until shared backend justified (staged design exists) | Phase 2B/2C |
| 10 | **Edge update/rollback immaturity** [FACT — SSH scripts, gateway-update queue, legacy webapp; no staged rollout, no auto-rollback, no update attestation] | A bad agent release could brick fleet segments; recovery is manual SSH | Medium-high at 500+ gateways | Versioned artifact channel + canary cohort + self-verify/rollback in agent; build on existing `GatewayUpdateRequest` machinery | Before 1,000 gateways |

Honorable mentions [FACT]: `main.py` monolith (split by router when it next grows); Dockerfile self-migration race (single-line fix, deploy-coordinated); no CI pipeline (the excellent test suite runs only when someone remembers); dual schema lineage in `supabase/migrations/` (archive it).

---

## 5. Scaling Review

| Dimension | Verdict | Evidence / condition |
|---|---|---|
| Users | **Mostly ready** | Stateless JWT auth, throttled telemetry writes [FACT]. Needs login rate limits and pagination beyond ~50 concurrent operators [INFERENCE]. |
| Organizations | **Needs improvement** | Model exists; nullable `Site.organization_id`, opt-in scoping fallback [FACT]. Blocker for multi-tenant, not for single-tenant. |
| Sites | **Ready** | Indexed, scoped, metadata-rich [FACT]. |
| Gateways | **Mostly ready** | Heartbeat path lean after Phase 2; retention bounded; composite indexes in place [FACT]. Ceiling unmeasured; listing unpaginated [FACT]. ~1,000 plausible on current design [INFERENCE — requires staging Level 2/3 confirmation]. |
| Devices | **Mostly ready** | Gateway-scoped inventory with lifecycle states [FACT]. Workspace renders all rows client-side [FACT] — UI, not DB, is the limit [INFERENCE]. |
| BACnet objects | **Mostly ready** | Same as devices; per-port lock caps per-gateway read throughput [FACT/INFERENCE]. |
| Trend storage | **Needs improvement** | Hardened ingestion; indexed retrieval; retention [FACT]. Millions/day needs partitioning, typed values, aggregation — Phase 3 by design [FACT — roadmap]. |
| Jobs | **Mostly ready** | Locked claiming, composite index [FACT]. Needs stale-job reaping (risk #4). |
| Heartbeats | **Ready** (to measured limits) | Retention + indexes + reduced writes [FACT]; volume model documented. |
| Tunnel sessions | **Architectural blocker** (for horizontal scale) | Process-local registries [FACT]. Fine single-instance; extraction boundary documented. |
| Concurrent users | **Needs improvement** | No pagination on fleet lists; summary endpoint loads all rows [FACT]. Fine ≤ dozens [INFERENCE]. |
| API requests | **Mostly ready** | Bounded payloads, deterministic ordering, timing logs [FACT]; single sync worker ceiling unmeasured. |
| Database | **Mostly ready** | Pool controls, pre-ping, recycle [FACT]; Supabase pooler compatibility unverified [FACT — flagged UNKNOWN]. |
| Memory | **Unknown** | Tunnel buffers whole proxied bodies in memory [FACT — base64 in `tunnel.py`]; no measurements. |
| CPU | **Unknown** | No profiling; sync workers + SQL-light handlers suggest I/O-bound [INFERENCE]. |
| Network | **Ready** | Edge traffic is small JSON; tunnel is the only heavy path [FACT]. |

---

## 6. Security Review

**Authentication.** [FACT] Three planes, cleanly separated: gateway bearer tokens (peppered HMAC, prefix-indexed, constant-time compare, revocation/expiry), Supabase JWTs (HS256 secret + JWKS for RS256/ES256, audience-checked), static admin token (constant-time compare). Sound design. Gap: no login throttling; JWKS client now cached (Phase 2).

**Authorization.** [FACT] Role gate (viewer/operator/admin) layered under site/organization scoping via centralized helpers; scope misses return 404. Phase 2 closed the two legacy bypasses. Gaps: the zero-membership fallback (risk #1); viewer-vs-operator distinctions are enforced but untested per-resource in places [INFERENCE from test inventory].

**Gateway trust.** [FACT] Tokens are per-gateway, scope-carrying, and useless cross-environment when peppers differ; every edge route re-checks token↔gateway_id binding. The tunnel strips inbound cloud Authorization headers and targets only allowlisted local routes on `127.0.0.1:5000`. Residual: a compromised gateway can only affect its own data — good blast-radius design.

**Secrets.** [FACT] No live secrets in repo; `.env.example` uses placeholders; docs prohibit committing keys; browser receives only public config. Gap: no secret-rotation runbook; pepper rotation would invalidate all gateway tokens simultaneously [INFERENCE — needs a dual-pepper verification window when it matters].

**Tenant isolation.** Covered at risk #1. Single-tenant today: acceptable. Multi-tenant: the fallback must die first.

**Auditability.** [FACT] BACnet write batches are fully audited; nothing else is. `security-model.md` already specifies the missing `audit_events`. This is the cheapest high-value security work remaining.

**Privilege boundaries.** [FACT] Admin token > admin role > operator > viewer > pending/disabled; edge tokens cannot reach UI/admin routes; UI tokens cannot reach edge routes. Clean.

**Attack surface.** [FACT] 76 HTTP routes + WebSocket + tunnel proxy. The tunnel proxy is the most exotic surface (HTML/JS URL rewriting in `main.py`) — it is allowlisted and session-gated, but it is hand-rolled HTML rewriting and deserves fuzzing before commercial exposure [RECOMMENDATION].

**Rate limiting.** Absent; staged design documented (Phase 2 §6). Login and provisioning first.

**Credential lifecycle / rotation.** [FACT] Columns and revocation exist; issuance is one-time-display; `last_used_at` tracked (now throttled). Missing: rotation workflows (re-issue + overlap window), expiry defaults (tokens are non-expiring unless set), automated revocation on gateway decommission [RECOMMENDATION — build on existing columns; no schema change needed].

---

## 7. Operational Readiness (50 customers / 500 customers / 5,000 gateways)

**Present today.** [FACT] Heartbeat telemetry with resource + backlog health; gateway update request queue with claim/complete; install/update/provision shell scripts; clone-safe provisioning docs; legacy-edge upgrade webapp; `/health*`; request-timing logs; staging prep suite.

**Missing, in priority order** [RECOMMENDATION, sized against the 5,000-gateway assumption]:

1. **Alerting** — nothing notifies anyone of anything. Offline-gateway, backlog-age, and error-rate alerts are prerequisites for the *first* customer SLA, not the 500th.
2. **Fleet health dashboard** — the data exists in `edge_nodes`; the fleet-wide view (version distribution, offline counts, backlog outliers) does not.
3. **Staged rollout + rollback for the edge agent** — cohort-based updates with automatic health verification and rollback; today an update is fleet-wide-or-manual over SSH.
4. **Version & configuration inventory** — agent/UI versions are reported [FACT]; there is no drift report, no desired-vs-actual reconciliation, no config snapshot per gateway.
5. **Diagnostics bundles** — one-click capture of edge logs/SQLite stats/heartbeat journal for support; today it is SSH spelunking.
6. **Support tooling / impersonation guardrails** — scoped, audited support access to a customer's view.
7. **Error aggregation** (Sentry-class) and the deferred metrics backend.
8. **Backup/DR posture** — Supabase backups exist platform-side [INFERENCE]; there is no documented restore drill, RPO/RTO statement, or export tooling.

At 50 customers you can survive with items 1–2 and heroics; at 500 you need 1–6; at 5,000 gateways all eight plus the tunnel extraction. [INFERENCE]

---

## 8. Release Readiness

**Verdict: Release 0.1 is production-ready as a single-tenant, operator-supervised commissioning tool — which is exactly how it is being used [FACT — live Render deployment]. It is not ready for first commercial (multi-customer) deployment.**

Required before first commercial deployment [RECOMMENDATION]:

1. Tenant fallback retirement + boundary test suite (risk #1).
2. Staging environment validated (prep complete; execute worksheet + trend checklist + load Levels 1–3).
3. Stale-job reaping, duplicate-result guard (risks #4).
4. Audit events for sensitive actions (risk #6); demote admin token to break-glass (risk #5).
5. Login rate limiting; token expiry defaults for new gateway credentials.
6. Offline-gateway alerting (minimum viable: email/webhook on transition).
7. CI pipeline running the existing suites on every change.
8. Set `ENVIRONMENT=production` on the production service; remove Dockerfile self-migration in favor of pre-deploy (single small change, deploy-coordinated).
9. Restore drill for the production database, documented.
10. Written SLO baseline from staging measurements.

Explicitly *not* required for 0.1-commercial: tunnel extraction (stay single-instance deliberately), frontend rewrite (freeze `ui.py` instead), partitioning, Redis, brokers. [RECOMMENDATION]

---

## 9. Technical Debt

**Must fix now (blocks correctness or first customer):** tenant fallback; stale-job reaper; audit events; CI; Dockerfile migration authority; production `ENVIRONMENT` flag.

**Fix before enterprise (500 customers / 1,000+ gateways):** tunnel service extraction; portal frontend replacing `ui.py`; typed trend values; rate limiting with shared backend; staged edge rollout/rollback; pagination on fleet endpoints; `main.py` split by router; pepper/token rotation workflows; error aggregation + metrics backend; `ui_list_gateway_updates` N+1 and missing-gateway abort.

**Can safely defer:** `supabase/migrations/` archival (governed already — archive when convenient); per-sample idempotency SELECT batching (fine ≤500/batch); weather-fetch async conversion; Windows/Linux test-stub friction (`test_bacnet.py` exec-bit, `tomllib`); request-body compression.

**Never worth fixing:** replacing the Postgres job queue with a broker at current contention profiles; rewriting the edge loop as async/multi-worker (its simplicity *is* the reliability feature); converting the CLI BACnet wrappers to a native stack before COV/alarms force it — the wrappers are field-proven [FACT — production use] and the abstraction boundary (`bacnet.py`) makes later replacement contained.

---

## 10. Future Architecture (roadmap critique)

The planned order — Phase 3 Trend Engine → Phase 4 Visualization → Phase 5 AI → Phase 6 Enterprise — is **wrong in two places and missing two phases**. [RECOMMENDATION]

**Reorder: pull Enterprise tenancy forward, push AI back.** Phase 6's tenant/fleet substance is a *precondition* for selling to anyone, while Phase 5's AI is valueless until the data model carries meaning. Sequence tenancy (2C) and fleet ops (2B/6-partial) before the trend engine grows, because retrofitting isolation onto a bigger dataset only gets harder.

**Missing phase A: Frontend extraction.** Phase 4 as scoped (zoom, aggregation, multi-series, exports) is not buildable inside `ui.py` string templates at acceptable cost. The Vercel portal already exists as intent in `docs/architecture.md` [FACT]; make it a named phase gating Phase 4, not an aspiration.

**Missing phase B: Semantic/equipment model.** The Foundation Assessment [FACT] and this review agree: there are no equipment entities, no point-to-equipment mapping, no semantic tags (Haystack/Brick-style), no units discipline (a `units` column exists on points, unpopulated by any pipeline [FACT]). Phase 5 AI (FDD, root-cause, digital twins) is impossible without it, and Phase 4 visualization is much richer with it. Insert between trend engine and visualization.

**Split Phase 3.** Do "typed values + retention/aggregation worker + partitioning readiness" first (unblocks everything), and "query engine / downsampling API" second (needed by Phase 4). The first half is small; the second should be driven by the portal's actual query patterns.

**Also missing entirely: alarms/events.** No commercial BAS platform ships without alarm ingestion, acknowledgment, and notification [INFERENCE — competitive table stakes; the dashboard already has a placeholder Event Stream region [FACT]]. Schedule it no later than the semantic-model phase.

Revised order: **2B/2C (jobs, tenancy, audit, alerts) → Staging/measurement → Phase 3a (typed trends + retention) → Frontend extraction → Semantic model + alarms → Phase 3b/4 (query engine + visualization) → Phase 6 fleet/enterprise completion → Phase 5 AI.**

---

## 11. Fleet Operations Review

**Provisioning: mostly ready.** [FACT] Cloud-side provision endpoint with one-time token, clone-safe gateway scripts, unattended-friendly. Gap: no bulk provisioning, no hardware attestation.

**Updates: needs improvement.** [FACT] `GatewayUpdateRequest` queue + `update-edge-agent.sh` + legacy upgrade webapp work for tens of gateways. No cohorts, no automatic post-update health gate, no artifact signing.

**Rollback: missing.** No previous-version retention on the gateway, no automatic revert on failed health check. [FACT — scripts install in place]

**Monitoring: partial.** Rich per-gateway telemetry [FACT]; no fleet aggregation, no alerting (§7).

**Diagnostics: manual.** SSH only. The tunnel could serve read-only diagnostics safely — it already proxies the local UI [FACT]; a diagnostics-bundle endpoint on the edge would be additive [RECOMMENDATION].

**Configuration drift: unmanaged.** Edge config is a local file; the cloud neither snapshots nor reconciles it. Desired-state config (cloud-defined, edge-applied, drift-reported) is the right long-term model and fits the existing job mechanism [RECOMMENDATION].

**Version management: partial.** Versions reported in heartbeats and surfaced per-gateway [FACT]; no fleet distribution view or minimum-version policy.

**Supportability / remote troubleshooting: partial.** Tunnel gives eyes-on-gateway [FACT — genuinely differentiating]; lacks session audit and scoped support roles.

**Gateway lifecycle: partial.** Inventory lifecycle states exist for devices/points [FACT — `INVENTORY_LIFECYCLE.md`]; gateways themselves lack decommission/replace workflows (credential auto-revoke, data retention decisions).

**Verdict: ready for ~50 supervised gateways; needs items above for hundreds; needs all of it plus staged rollout for thousands.** [INFERENCE]

---

## 12. AI Readiness

**Question: does the data model support FDD, predictive maintenance, LLM analysis, NL queries, root-cause, digital twins without major redesign? Answer: the skeleton yes, the flesh no — and the gaps are additive, not structural.** [INFERENCE]

What supports it [FACT]: stable point identity (gateway/device/object/property with lifecycle states); time-series with quality/source/timestamps and idempotent ingestion; heartbeat/resource telemetry as a parallel machine-health stream; jobs as a structured actuation record; write audit as intervention history; weather joined at site level.

What blocks it [FACT]: string values (no numeric aggregation); no units pipeline; no equipment/relationship graph; no semantic tags; no alarms/events stream; no notes/annotations (human labels are FDD training gold); COV absent so temporal resolution is polling-bound.

**No major redesign is required** because every gap is an additive layer over stable keys: typed value columns beside the string; an `equipment` + `point_roles` graph referencing existing point IDs; tag tables; an events stream keyed the same way. The Foundation Assessment reached the same conclusion independently [FACT]. The one decision to take *now* so AI stays cheap later: **never reuse point IDs, never mutate `sampled_at` semantics, and populate `units`** — identity stability is the whole game [RECOMMENDATION].

---

## 13. Commercial Readiness

Missing capabilities versus commercial BAS cloud platforms, prioritized [RECOMMENDATION; competitive claims are INFERENCE]:

1. **Alarms/events with notification** — table stakes; absence is disqualifying in most evaluations.
2. **Multi-tenant administration** — org onboarding, member invitations, roles per org, tenant deletion; today this is admin-token surgery.
3. **Modern portal UX** — evaluations are won in the browser; inline-string UI cannot compete on polish or velocity.
4. **Reporting/exports** — commissioning reports, trend exports, compliance PDFs; the commissioning-template import exists, report generation does not.
5. **Scheduling** — BACnet schedule *objects* are recognized [FACT — object-type map only]; schedule read/write UX does not exist.
6. **Alerting/SLA machinery** — customer-facing uptime and gateway-offline notifications.
7. **SSO (SAML/OIDC)** — enterprise procurement gate; Supabase Auth gives a path.
8. **Compliance posture** — SOC 2 trajectory: audit log, access reviews, backup/DR evidence, secrets policy (much of the documentation culture here is already ahead of typical).
9. **Integrations** — Niagara/Modbus/MQTT ingestion, or at minimum a documented ingestion API for non-BACnet sources (MQTT is already in the long-term vision [FACT]).
10. **Point normalization/tagging UX** — the semantic layer of §10/§12 surfaced as a product feature.
11. **Billing/entitlements** — later, but the org model should not preclude per-gateway metering [INFERENCE — it does not today].

Differentiators already in hand worth protecting [FACT]: zero-inbound-network deployment, the gateway tunnel (remote eyes on local UI without site visits), staged BACnet write approval, and offline-tolerant telemetry.

---

## 14. Final Recommendation — CTO 12-Month Plan

**Months 1–2 — Trust the ground you stand on.** Stand up staging (prep complete); run first-deploy worksheet, trend checklist, load Levels 1–3; wire CI running all suites; fix the four "must fix now" code items (tenant fallback, job reaper, audit events, migration authority). Exit: measured capacity numbers replace every PROJECTED label; isolation is default-on.
**Why first:** every later decision (pool sizes, instance counts, pricing floors) is guesswork until measured, and tenancy debt compounds with every row written.

**Months 3–4 — Sellable single-tenant → safe multi-tenant.** Org onboarding/invitations, per-resource boundary tests, login rate limiting, token expiry defaults, offline-gateway alerting, error aggregation. First design pass on alarms/events model. Exit: two demo tenants coexist provably; a gateway going dark pages someone.

**Months 5–6 — Fleet operations.** Fleet dashboard (version distribution, offline, backlog outliers); staged edge rollout with health-gated auto-rollback; diagnostics bundle; gateway decommission workflow. Exit: a 200-gateway fleet is operable by one person.

**Months 7–8 — Data engine (Phase 3a).** Typed trend values with dual-write migration, units pipeline, retention/aggregation worker (first true background service — deploy as the second Render service and use it to validate the service-separation pattern ahead of the tunnel). Exit: numeric aggregation in SQL; retention no longer piggybacks on request paths.

**Months 9–10 — Frontend extraction + tunnel service.** Vercel portal for the top five workflows (fleet, gateway detail, workspace, trends, admin); freeze `ui.py`; extract tunnel to its own service, unlocking multi-instance API. Exit: API scales horizontally; UI velocity decoupled from Python deploys.

**Months 11–12 — Semantic model + alarms (Phase 5 prerequisites).** Equipment/point-role graph, tagging UX, alarm ingestion/ack/notify MVP, trend query/downsampling API. Exit: the platform can *mean* something about a building, which is the doorway AI actually walks through.

**AI itself ships in year two, on purpose.** Everything in months 1–12 is either revenue-enabling now or a hard prerequisite for AI that is more than a demo. That ordering — measurement, tenancy, operations, data, experience, semantics — is how this codebase becomes a commercial platform without betraying the restraint that makes it good.

---

*Repository untouched by this review except for this document. Nothing committed or pushed.*
