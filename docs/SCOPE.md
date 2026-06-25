# Full Scope: IOT Cloud Commissioning

## 1. Scope Summary

IOT Cloud Commissioning is a cloud-to-edge platform for securely provisioning BACnet commissioning gateways, monitoring their status, queueing safe commissioning jobs, and storing results for operator review and future commissioning reports.

The scope includes:

- cloud API
- Supabase/Postgres schema
- edge gateway agent
- clone-safe gateway provisioning
- operator/admin API security
- Supabase email login and admin user roles
- operator UI foundation
- BACnet runtime checks and read jobs
- future reports and evidence
- future role-based access control

The scope explicitly excludes any work that would modify or interfere with the legacy UDP `47808` runtime.

## 2. Architecture Scope

### 2.1 In Scope

The system includes the following components:

1. FastAPI cloud API
2. Supabase Postgres database
3. Render-hosted dev deployment
4. Edge gateway agent
5. Edge gateway local SQLite database
6. BACnet-stack local CLI execution on gateway
7. Gateway credential provisioning and validation
8. Operator/admin API protection
9. Operator UI foundation
10. Future report/evidence storage

### 2.2 Out of Scope

The system does not include:

1. Cloud execution of BACnet commands
2. Direct edge gateway connection to Supabase/Postgres
3. Supabase service-role keys on gateways
4. Admin/operator tokens on gateways
5. FastAPI/cloud-api installation on production gateways
6. Modification of legacy UDP `47808` runtime
7. Uncontrolled BACnet writes
8. Unauthenticated public job creation

## 3. Critical Architecture Rules

These are non-negotiable:

- Edge agent calls FastAPI only.
- Edge agent must never connect directly to Supabase/Postgres.
- Edge agent must never use Supabase service-role keys.
- FastAPI/cloud-api must not be installed on production gateways.
- The cloud/server pepper must never be installed on an edge gateway.
- Gateway token may be installed on edge gateway only in `/etc/iot-cx-agent/edge-agent.env`.
- `IOT_ADMIN_API_TOKEN` belongs only in Render/admin/operator context, never on gateways.
- BACnet execution stays local on edge gateway.
- Legacy runtime uses UDP `47808` and must not be touched.
- Cloud/commissioning BACnet runtime uses UDP `47814`.
- Shared BACnet lock path must remain `/tmp/iot-cloud-commissioning-bacnet-47814.lock`.

## 4. Delivered Scope Through MVP-012

### 4.1 MVP-001 Heartbeat Foundation

Delivered:

- Edge heartbeat API
- Gateway status model
- Initial gateway visibility
- Edge agent heartbeat behavior
- Basic tests

### 4.2 MVP-002 Cloud-to-Edge Jobs

Delivered:

- Edge jobs table/model
- Create job endpoint
- Claim next job endpoint
- Post result endpoint
- Edge polling loop
- Echo job handler
- Local SQLite job history
- Tests and runtime smoke

### 4.3 MVP-003 BACnet Discovery Job

Delivered:

- BACnet discovery job framework
- Local BACnet command execution
- BACnet runner/parser
- Timeout/error handling
- Tests

### 4.4 MVP-004 Supabase Readiness

Delivered:

- Supabase plan
- Security model
- API contract docs
- Migration skeleton
- Architecture docs

### 4.5 MVP-005 Supabase FastAPI

Delivered:

- Supabase Postgres connection from FastAPI
- Health DB endpoint
- Migrations applied
- Tests passing
- Edge remained SQLite-only

### 4.6 MVP-006 Gateway Authentication

Delivered:

- Gateway identity table
- Gateway credential table
- Gateway auth flow
- Gateway token validation
- Last-used timestamp update
- Live smoke covering no token, valid token, wrong gateway token

### 4.7 MVP-011 Clone-Safe Gateway Provisioning

Delivered:

- Clone master preparation workflow
- Hostname reset to unprovisioned state
- Gateway ID/site ID reset
- Gateway token removed
- Edge DB removed
- Edge agent disabled/inactive
- BACnet `47814` lock checked clear
- Clone image created
- Clone provisioned as `GW777`
- `GW777` heartbeat accepted
- `GW777` runtime check completed
- `GW777` BACnet read completed

### 4.8 MVP-012 Operator API Security

Delivered:

- `IOT_ADMIN_API_TOKEN` operator/admin auth
- Protected operator endpoints:
  - `GET /api/edge/gateways`
  - `POST /api/edge/jobs`
  - `GET /api/edge/jobs`
  - `POST /api/admin/gateways/provision`
- Gateway auth unchanged for:
  - `POST /api/edge/heartbeat`
  - `GET /api/edge/{gateway_id}/jobs/next`
  - `POST /api/edge/jobs/{job_id}/result`
- Initial Render deployment after admin token was added
- Deployed commit `11c8b1f`
- Swagger exposes `AdminBearer (http, Bearer)`
- Admin/operator auth works through Swagger Authorize with `IOT_ADMIN_API_TOKEN`
- `POST /api/edge/jobs` uses `request`, not `payload`
- Safe runtime-check request uses `request.bacnet_port = 47814`

## 5. Immediate Stabilization Scope

### 5.1 MVP-012 Smoke Completion

Status:

- Completed for admin/operator auth and gateway list smoke.
- `GW777` is visible through authenticated `GET /api/edge/gateways`.
- `GW777` is online.
- `GW777` uses BACnet port `47814`.
- Latest known queued smoke job at handoff: `job-3dcf32e743414f37be81d50d447a565b`.
- Latest known queued smoke job request JSON at handoff: `{ "bacnet_port": 47814 }`.

Goals:

- Confirm repo auth implementation
- Confirm tests cover admin/operator auth
- Confirm Bearer token contract
- Confirm OpenAPI/Swagger auth support
- Confirm Render environment matches code
- Complete live smoke with `GW777`

Acceptance criteria:

- `/health` returns OK
- `/health/db` returns OK
- Missing admin token returns `401`
- Invalid admin token returns `401`
- Valid Bearer admin token returns gateway list
- Gateway list includes `GW777`
- Gateway list shows `GW777` online
- Gateway list shows `GW777` using BACnet port `47814`
- Runtime-check job queues using `request`
- Runtime-check job uses `bacnet_port = 47814`
- `GW777` claims and completes job
- No `47808` interaction

### 5.2 Documentation Cleanup

Update:

- `AGENTS.md`
- smoke-test docs
- API contract docs
- security model
- README links as needed

Ensure:

- `POST /api/edge/jobs` uses `request`, not `payload`
- Admin auth is documented as `Authorization: Bearer <token>`
- Operator/admin token is not documented as a gateway secret
- UDP `47814` is the commissioning runtime port

## 6. Future Scope by Milestone

### 6.1 MVP-013 Supabase Email Login And Admin User Roles

Purpose:

Move human access away from a shared pasted admin token by adding Supabase email identity and local app roles.

Features:

- Supabase email/password signup.
- Email confirmation handled by Supabase.
- Email address is the username.
- FastAPI verifies Supabase user JWTs using `SUPABASE_JWT_SECRET` for legacy `HS256` or Supabase JWKS for `RS256`/`ES256`.
- Local `operator_users` records store role and status.
- New users are `pending` until approved.
- Roles: `admin`, `operator`, `viewer`, `pending`.
- Statuses: `active`, `pending`, `disabled`.
- Admin user-management API for assigning roles.
- Browser login and signup pages.
- Confirmation-required page.
- Signup confirmation redirect uses `${window.location.origin}/login`.
- Waiting-for-approval page.
- Unauthorized page.
- Protected app dashboard route.
- Session-based admin users page.
- Existing `IOT_ADMIN_API_TOKEN` remains for scripts, smoke tests, and emergency automation.

Not included:

- Full polished browser portal build.
- Direct browser access to privileged database tables.
- BACnet writes
- Report generation
- Gateway token rotation

Acceptance criteria:

- Confirmed Supabase user can register a pending app profile.
- Pending user cannot call operator routes.
- Admin can assign user roles and statuses.
- Active operator can view gateways and queue jobs.
- Viewer is read-only.
- Admin-only endpoints reject non-admin users.
- Unauthenticated users are redirected away from protected pages.
- No edge gateway receives user, admin, or Supabase credentials.
- Supabase Auth Site URL is `https://iot-cloud-api-dev.onrender.com`.
- Supabase redirect allow list includes production app URLs such as `https://iot-cloud-api-dev.onrender.com/login`.

### 6.2 MVP-014 Commissioning Job Workflows

Purpose:

Add practical commissioning workflows beyond basic runtime check.

MVP-014A foundation:

- Operator dashboard gateway list.
- Per-gateway workspace route.
- Effective gateway status derived from heartbeat age:
  - `online` when heartbeat is recent.
  - `stale` when heartbeat is old.
  - `offline` when heartbeat is missing or expired.
- Gateway status summary counts.
- Saved gateway groups.
- Saved BACnet device metadata.
- Saved BACnet point metadata.
- Safe BACnet discovery job queueing for online gateways only.
- Discovery jobs use `request.bacnet_port = 47814`.
- Viewer read-only access to gateway UI state.
- Operator/admin write access for group/device/point metadata and safe job queueing.

Not included in MVP-014A:

- BACnet writes.
- Direct cloud execution of BACnet.
- Faked point lists.
- Automatic point enumeration unless the edge agent has a tested job for it.

Features:

- BACnet runtime check
- BACnet device discovery
- BACnet property read
- Batch property read where safe
- Job templates
- JSON result viewer
- CSV export
- Error display
- Timeout display

Acceptance criteria:

- Operator can queue approved job types only
- Jobs execute locally on gateway
- Results are stored and visible
- BACnet port remains `47814`
- Failed jobs include useful error messages without secrets
- Stale or offline gateways are not displayed as active solely because the last stored status was `online`

### 6.3 MVP-015 Gateway Lifecycle Management

Purpose:

Manage gateway lifecycle from provisioning through decommissioning.

Features:

- Provision gateway
- Display token prefix
- Rotate gateway token
- Revoke gateway token
- Decommission gateway
- Clone preparation checklist
- Clone validation checklist
- Gateway stale/offline status

Acceptance criteria:

- Raw gateway token shown only once
- Token prefix visible for support
- Revoked token no longer works
- Decommissioned gateway cannot claim jobs
- Clone-safe state can be validated

### 6.4 MVP-016 Commissioning Evidence and Reports

Purpose:

Generate customer-ready commissioning records.

Features:

- Job evidence bundle
- Gateway summary report
- Site commissioning report
- Export JSON
- Export CSV
- Future PDF report
- Evidence storage path metadata

Acceptance criteria:

- Operator can download evidence for a run
- Reports include gateway, site, job, timestamps, and results
- Reports exclude secrets
- Report data is reproducible from stored job results

### 6.5 MVP-017 Role-Based Access Control

Purpose:

Replace or supplement single admin token with user-based access.

Features:

- User login
- Roles
- Site permissions
- Operator/admin separation
- Audit trail
- Session management

Candidate roles:

- admin
- operator
- viewer
- support

Acceptance criteria:

- Users can only access assigned sites
- Sensitive actions are audited
- Admin-only actions are protected
- Gateway auth remains separate from user auth

### 6.6 MVP-018 Realtime Status and Notifications

Purpose:

Improve live operator visibility.

Features:

- Realtime gateway status
- Job progress updates
- Job completion notifications
- Stale gateway alerts
- Failed job alerts

Acceptance criteria:

- Operator sees status changes without manual refresh
- Failed jobs are visible quickly
- Alerts do not expose secrets

## 7. API Scope

### 7.1 Public Health Endpoints

In scope:

- `GET /health`
- `GET /health/db`

Purpose:

- deployment verification
- database connectivity check

### 7.2 Gateway Endpoints

In scope:

- `POST /api/edge/heartbeat`
- `GET /api/edge/{gateway_id}/jobs/next`
- `POST /api/edge/jobs/{job_id}/result`

Auth:

- gateway token only

### 7.3 Operator/Admin Endpoints

In scope:

- `GET /api/edge/gateways`
- `POST /api/edge/jobs`
- `GET /api/edge/jobs`
- `POST /api/admin/gateways/provision`

Auth:

```text
Authorization: Bearer <IOT_ADMIN_API_TOKEN>
```

### 7.4 Future API Endpoints

Candidate future endpoints:

- `POST /api/admin/gateways/{gateway_id}/rotate-token`
- `POST /api/admin/gateways/{gateway_id}/revoke`
- `GET /api/sites`
- `POST /api/sites`
- `GET /api/commissioning/runs`
- `POST /api/commissioning/runs`
- `GET /api/commissioning/runs/{run_id}/evidence`
- `GET /api/audit/events`

## 8. Data Scope

### 8.1 Current Data Entities

- edge nodes
- gateway credentials
- edge jobs

### 8.2 Future Data Entities

- sites
- operator users
- audit events
- job events
- commissioning projects
- commissioning templates
- commissioning runs
- commissioning evidence

## 9. Security Scope

### 9.1 In Scope

- Gateway token hashing
- Token prefix display
- Gateway token last-used timestamp
- Admin/operator token protection
- Protected operator routes
- Separate gateway and operator auth models
- Render environment variables
- No secrets in source control
- No admin tokens on edge

### 9.2 Future Security Scope

- User auth
- Roles
- Site permissions
- Audit log
- Token rotation UI
- Secret scanning in CI
- Rate limiting
- Request logging without secrets

## 10. Testing Scope

### 10.1 Current Required Tests

Admin/operator auth tests:

- missing token returns `401`
- invalid token returns `401`
- valid Bearer token returns `200`
- raw token rejected unless contract changes
- all protected endpoints require admin/operator auth

Gateway auth tests:

- heartbeat requires correct gateway auth
- job claim requires matching gateway credentials
- job result requires gateway auth
- gateway cannot impersonate another gateway

BACnet safety tests:

- runtime check defaults to `47814`
- job body examples use `request`
- no test or default uses `47808` for cloud commissioning runtime
- lock path is correct

### 10.2 Future Tests

- UI route and role tests
- stale/offline effective status tests
- gateway group/device/point tree tests
- provisioning lifecycle tests
- token rotation tests
- report generation tests
- audit event tests

## 11. Deployment Scope

### 11.1 Current Deployment

- Render hosts FastAPI dev service
- Supabase hosts Postgres
- Edge gateways run edge agent
- Edge gateways execute BACnet locally

### 11.2 Deployment Rules

- Render receives `IOT_ADMIN_API_TOKEN`
- Render receives cloud database connection settings
- Gateway receives gateway API token only
- Gateway does not receive Render admin token
- Gateway does not receive Supabase service-role key
- Gateway does not receive cloud/server pepper

## 12. Operator Smoke Scope

The standard smoke test includes:

1. Check `/health`
2. Check `/health/db`
3. Confirm missing admin token returns `401`
4. Set local admin token using `Read-Host`
5. Confirm gateway list returns `200`
6. Confirm `GW777` appears
7. Queue `GW777` runtime check with `request.bacnet_port = 47814`
8. Poll jobs until completed
9. Confirm result references `47814`
10. Stop if any step shows `47808`

Latest known handoff state:

- Gateway: `GW777`
- Gateway status: `online`
- Gateway BACnet port: `47814`
- Smoke job: `job-3dcf32e743414f37be81d50d447a565b`
- Smoke job status: `queued`
- Smoke job request JSON: `{ "bacnet_port": 47814 }`

## 13. Exclusions

Excluded unless explicitly approved later:

- BACnet write jobs
- Schedule writes
- Program object editing
- Router replacement features
- Direct field device control from UI
- Customer-facing login
- Billing features
- Inventory purchasing
- Remote OS patch management
- General-purpose VPN tooling
- Legacy UDP `47808` modifications

## 14. Deliverables

### 14.1 Current Documentation Deliverables

- PRD
- ERD
- Full scope
- Agent instructions
- Smoke-test procedure
- API contract
- Security model

### 14.2 Current Engineering Deliverables

- Protected API endpoints
- Gateway auth
- Gateway provisioning
- Clone-safe workflow
- BACnet runtime check
- BACnet read job
- Tests
- Live smoke proof

### 14.3 Future Deliverables

- Operator UI
- Gateway lifecycle UI
- Commissioning workflow UI
- Report generation
- Audit trail
- Role-based access control
- Realtime dashboard

## 15. Acceptance Boundary

The current phase is considered complete when:

- MVP-012 live admin smoke passes
- Swagger exposes `AdminBearer (http, Bearer)`
- Codex agent instructions are committed
- PRD/ERD/scope docs are committed
- API docs reflect Bearer auth or clearly document header auth
- job body examples use `request`
- no forbidden naming or legacy runtime changes are introduced
- all relevant tests pass
