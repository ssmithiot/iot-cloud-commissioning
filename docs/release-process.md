# Release Process — IoT Cloud Commissioning

Applies to every cloud release once staging exists. Scope: 160-site initial fleet, designed not to dead-end at ~1,300. One release at a time; no parallel release trains at this fleet size.

Referenced companions: `production-readiness-checklist.md` (gate before step 9), `staging-first-deploy-worksheet.md` (step 4–6 commands), `staging-trend-validation-checklist.md` (step 5), `cloud-platform-staging-load-test-plan.md` (step 7), `disaster-recovery-runbook.md` (step 11), `gateway-field-guide.md` (edge releases).

## Lifecycle

### 1. Development
- Work on a feature branch per the phase-charter pattern; all changes local until review.
- Every schema change ships as one guarded, reversible Alembic migration + model + tests in the same change (per `docs/architecture/SCHEMA_GOVERNANCE.md`).
- No change may touch tunnel implementation or BACnet write behavior without explicit approval.

### 2. Local Verification
Run before any deploy is even discussed. All must pass:

```bash
# from repo root (Windows: use the venv python)
python -m pytest cloud-api/tests -q
python -m pytest edge-agent/tests -q
python -m pytest tools/tests -q
```

- Known environment-specific exclusions are documented in the Phase 2 report (`test_bacnet.py` exec-bit stubs are Linux-hostile; `test_version.py` needs Python ≥3.11). On the development Windows machine the full suite is expected green except the one pre-existing `test_gateway_workspace_contains_discovery_progress_ui` failure (tracked in the debt register).
- Migration round-trip on a scratch SQLite database: `alembic upgrade head`, `alembic downgrade -1`, `alembic upgrade head`.

### 3. Staging Deployment
- Deploy the release candidate ref to the staging Render service (manual deploy of the branch/tag — never auto-deploy from main).
- Pre-deploy hook runs `alembic upgrade head` against staging Supabase.
- Confirm startup: no "known production resources" refusal, no schema-drift refusal.

### 4. Smoke Tests (staging)
Run the release smoke checker (or the worksheet manually):

```bash
python tools/release_smoke_check.py --base-url https://<staging>.onrender.com \
  --expect-environment staging --admin-token $STAGING_ADMIN_TOKEN --synthetic
```

Covers: health/identity/version, DB health, schema head match, auth accept/reject, synthetic gateway provision → heartbeat → job round-trip → trend upload/retrieve. Pass = exit code 0, JSON report archived.

### 5. Trend Validation (staging)
Execute `staging-trend-validation-checklist.md` in full on first release and after any release touching trends, ingestion, retention, or edge upload paths; otherwise the smoke checker's trend round-trip suffices.

### 6. Gateway Validation (staging)
- At least one real (non-production) edge device pointed at staging, running the release-candidate agent, for ≥30 minutes: heartbeats green, job execute, trend upload, backlog zero.
- If the release changes the edge agent: also run one update-in-place via `scripts/update-edge-agent.sh --ref <candidate>` against the staging test gateway and verify recovery.

### 7. Load Validation (staging)
- Required for: first release, any release touching DB engine/pooling/indexes/ingestion/claiming, and at least quarterly. Otherwise optional.
- Level 1 always; Level 2 for the required cases above (per the load-test plan). Archive harness JSON next to the release record.

### 8. Release Approval
- All release metrics (below) green, checklist signed, known-issues list updated.
- Approver: platform owner. Record: ref/tag, migration head, metrics evidence, approver, date — in the release log (start one at `docs/releases.md` on first production deploy).

### 9. Production Deployment
- Complete `production-readiness-checklist.md` — it is mandatory, every time.
- Deploy the *identical* ref that passed staging. Pre-deploy runs migrations; the app then verifies schema at startup and refuses drift.
- Deploy during business hours with the operator available for 60 minutes after (single instance = brief restart blip; tunnel consoles drop and must be reopened — expected behavior).

### 10. Post-Deployment Validation (production)
Within 10 minutes:

```bash
python tools/release_smoke_check.py --base-url https://iot-cloud-api-dev.onrender.com \
  --expect-environment production --read-only
```

Read-only mode checks health, environment identity, version, DB, schema head, and unauthenticated-rejection only — it provisions nothing and writes nothing. Then verify with real traffic: gateway heartbeats resuming across the fleet (spot-check 3 gateways in the UI), one trend chart renders with fresh samples, request logs show normal latencies for 30 minutes.

### 11. Rollback (if required)
Triggers: failed post-deploy validation, error-rate/latency regression, fleet heartbeat failures. Procedure per `disaster-recovery-runbook.md` §Render-rollback:
- Redeploy the previous known-good ref via Render (previous image redeploy).
- Migrations are additive/guarded by policy; a rollback deploy runs against the newer schema — this is safe for additive migrations, which is why the policy exists. If a migration must be reversed, run `alembic downgrade <prev>` deliberately, then redeploy — never automatically.
- Record the rollback and root cause in the release log before attempting again.

## Edge (gateway software) releases
Follow the same lifecycle with steps 4–7 replaced by the staging test-gateway update (step 6) plus a canary: update 2–3 production gateways first, observe 24 h of heartbeat/trend/job health, then roll the fleet in batches (`gateway-field-guide.md` §software-update). The gateway-update request queue tracks progress per gateway [`GatewayUpdateRequest`].

## Release Metrics — objective pass/fail gates

| Metric | How measured | Pass criterion |
|---|---|---|
| Migrations complete | `GET /health/schema` | `status:"ok"`, expected == current revisions |
| Health | `GET /health` | 200, `status:"ok"` |
| Environment identity | `GET /health` | `environment` equals expected target (`staging`/`production`) |
| Database connectivity | `GET /health/db` | 200 |
| Authentication accept | `GET /api/ui/gateways` with valid token | 200 |
| Authentication reject | same with invalid token | 401 |
| Authorization / tenant isolation | scoped-operator boundary tests in CI + staging worksheet §8 | tests green; worksheet 404s confirmed |
| Gateway heartbeat success | staging synthetic + production spot-check | synthetic 200s; ≥95% of fleet heartbeating within 2 intervals post-deploy |
| Trend upload success | smoke checker round-trip | upload 200, retry idempotent, retrieval shows sample |
| Job execution success | smoke checker round-trip | created → claimed exactly once → result stored |
| Test suite | CI/local run | 100% pass minus documented known exclusions |
| Resource utilization | Render dashboard 30 min post-deploy | CPU/memory within ±20% of pre-deploy baseline |
| Error rate | `iot-cloud-api.requests` logs 30 min post-deploy | 5xx < 1% of requests |

Any red metric = stop, do not proceed / roll back. No judgment calls on the gates themselves; judgment goes into fixing the cause.
