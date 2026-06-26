# Product Requirements Document: IOT Cloud Commissioning

## 1. Product Summary

IOT Cloud Commissioning is a cloud-to-edge commissioning platform for remote BACnet gateway deployment, verification, and operator-controlled commissioning workflows.

The system allows an operator to manage edge gateways, provision gateway identity and credentials, monitor gateway health, queue cloud-originated jobs, and receive verified job results from field gateways without giving the edge gateway direct database access.

BACnet execution remains local to the gateway. The cloud only queues work and stores results.

## 2. Current Product State

Released milestones:

| Milestone | Status |
|---|---|
| MVP-001 Heartbeat foundation | Released |
| MVP-002 Cloud-to-edge jobs | Released |
| MVP-003 BACnet discovery job | Released |
| MVP-004 Supabase readiness | Released |
| MVP-005 Supabase FastAPI | Released |
| MVP-006 Gateway authentication | Released |
| MVP-011 Clone-safe gateway provisioning | Released |
| MVP-012 Operator API security | Released |
| MVP-013 Supabase email login and admin user roles | Released |
| MVP-014A Operator UI foundation | In progress |
| MVP-014B Direct Connect and site management | Live smoke passed |
| MVP-014C BACnet point loading and point-tree population | Planned |

Current live dev API:

```text
https://iot-cloud-api-dev.onrender.com
```

Current deployed backend:

- FastAPI cloud API
- Supabase Postgres system of record
- Edge agent with local SQLite job history
- Gateway credential validation through FastAPI
- Operator/admin token protection for selected cloud API endpoints
- Supabase email-user auth foundation with local app roles
- Browser login/signup pages and protected app shell
- Operator dashboard and gateway workspace foundation
- Effective gateway status derived from heartbeat age
- Saved gateway groups, BACnet devices, and BACnet points data model
- Deployed commit `11c8b1f`
- Swagger exposes `AdminBearer (http, Bearer)` for protected operator endpoints

Current known gateway proof:

- Clone-safe provisioning workflow validated
- New clone provisioned as `GW777`
- `GW777` heartbeat accepted
- `GW777` completed `bacnet_runtime_check` on UDP `47814`
- `GW777` completed `bacnet_read` against device instance `1`, analog input `1`, present value
- Authenticated `GET /api/edge/gateways` through Swagger Authorize shows `GW777`
- `GW777` latest observed status is `online`
- `GW777` reports BACnet port `47814`
- Latest known smoke job at handoff: `job-3dcf32e743414f37be81d50d447a565b`
- Latest known smoke job status at handoff: `queued`
- Latest known smoke job request JSON at handoff: `{ "bacnet_port": 47814 }`

## 3. Product Goals

### 3.1 Primary Goals

1. Provide a safe cloud-to-edge commissioning workflow for BACnet gateways.
2. Allow operators to queue gateway jobs from cloud API or future UI.
3. Keep BACnet activity local to the gateway.
4. Keep edge devices isolated from cloud database credentials.
5. Support clone-safe gateway preparation and field provisioning.
6. Support auditable commissioning actions and results.
7. Preserve legacy BACnet runtime behavior on UDP `47808` by using UDP `47814` for cloud commissioning runtime.

### 3.2 Secondary Goals

1. Provide an operator UI for gateway status, jobs, and commissioning workflows.
2. Support downloadable commissioning evidence and reports.
3. Support role-based access control after the initial admin/operator token model.
4. Support trend uploads, point templates, and commissioning documentation.
5. Support future realtime gateway status updates.

## 4. Non-Goals

The product is not intended to:

1. Replace local BACnet controllers.
2. Execute BACnet operations in the cloud.
3. Give edge gateways direct Postgres or Supabase access.
4. Install the cloud API on field gateways.
5. Store admin/operator secrets on gateways.
6. Modify or interfere with the legacy UDP `47808` runtime.
7. Use service-role database keys on field gateways.
8. Act as a general-purpose public API without gateway/operator authentication.

## 5. User Personas

### 5.1 Operator / Commissioning Engineer

Needs to:

- View gateway status
- Confirm gateway heartbeat
- Queue safe jobs
- Review job results
- Confirm BACnet runtime health
- Capture evidence for commissioning records
- Avoid accidentally affecting active legacy systems

### 5.2 Field Technician

Needs to:

- Provision a cloned gateway
- Confirm gateway identity
- Confirm gateway agent health
- Confirm BACnet port and lock status
- Avoid handling cloud/admin secrets
- Run only clearly labeled commands

### 5.3 System Administrator

Needs to:

- Manage Render environment variables
- Manage Supabase database and migrations
- Rotate operator/admin tokens
- Control access and audit sensitive actions
- Maintain deployment health

### 5.4 Future Customer/User Viewer

Needs to:

- View gateway status
- View commissioning completion
- Download evidence reports
- See limited site-specific data only

## 6. Core Use Cases

### 6.1 Gateway Heartbeat

The edge gateway periodically sends heartbeat data to FastAPI.

Heartbeat includes:

- gateway ID
- site ID
- hostname
- LAN IP
- BACnet port
- agent version
- UI version
- SQLite health
- queued upload count
- timestamp

The cloud records latest status and heartbeat timestamps.

### 6.2 Gateway Provisioning

An operator provisions a gateway identity through a protected admin API.

Provisioning returns:

- gateway ID
- site ID
- hostname
- LAN IP
- BACnet port
- generated gateway API token
- token prefix

Gateway token is installed only on the gateway in:

```text
/etc/iot-cx-agent/edge-agent.env
```

### 6.3 Clone-Safe Gateway Preparation

A master gateway image can be prepared for cloning.

Required clone-safe state:

- hostname reset to unprovisioned state
- gateway ID reset to `UNPROVISIONED`
- site ID reset to `UNPROVISIONED`
- gateway token removed
- local edge database removed
- edge agent disabled/inactive
- BACnet UDP `47814` lock clear

### 6.4 Job Queueing

An authenticated operator queues a job for a gateway.

Example runtime-check body:

```json
{
  "gateway_id": "GW777",
  "job_type": "bacnet_runtime_check",
  "request": {
    "bacnet_port": 47814
  }
}
```

The edge agent polls FastAPI for the next job. The edge agent executes the job locally, then posts the result back to FastAPI.

### 6.5 BACnet Runtime Check

The gateway validates local BACnet runtime state without touching legacy UDP `47808`.

Runtime check confirms:

- BACnet port is `47814`
- shared lock is clear or properly held during execution
- local BACnet tools are executable
- expected tool paths exist
- no runtime errors were encountered

### 6.6 BACnet Read

The gateway performs local BACnet property reads through the cloud-queued job system.

The cloud stores:

- job status
- request JSON
- result JSON
- timestamps
- error message when applicable

## 7. Functional Requirements

### 7.1 Cloud API

The cloud API must provide:

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /health` | API health | Public |
| `GET /health/db` | Database health | Public |
| `POST /api/edge/heartbeat` | Receive gateway heartbeat | Gateway auth |
| `GET /api/edge/{gateway_id}/jobs/next` | Edge claims next job | Gateway auth |
| `POST /api/edge/jobs/{job_id}/result` | Edge posts job result | Gateway auth |
| `GET /api/edge/gateways` | Operator gateway list | Admin/operator auth |
| `POST /api/edge/jobs` | Operator creates job | Admin/operator auth |
| `GET /api/edge/jobs` | Operator lists jobs | Admin/operator auth |
| `POST /api/admin/gateways/provision` | Admin provisions gateway | Admin/operator auth |
| `GET /api/ui/gateways` | Browser UI gateway list with effective status | Viewer/operator/admin auth |
| `GET /api/ui/gateways/summary` | Browser UI gateway status summary | Viewer/operator/admin auth |
| `GET /api/ui/gateways/{gateway_id}/tree` | Browser UI imported commissioning model | Viewer/operator/admin auth |
| `POST /api/ui/gateways/{gateway_id}/groups` | Create saved gateway group | Operator/admin auth |
| `POST /api/ui/gateways/{gateway_id}/devices` | Save discovered BACnet device metadata | Operator/admin auth |
| `POST /api/ui/devices/{device_id}/points` | Save BACnet point metadata | Operator/admin auth |
| `POST /api/ui/gateways/{gateway_id}/commissioning-template/import` | Import an edge-exported commissioning template into the cloud commissioning model | Operator/admin auth |
| `POST /api/ui/gateways/{gateway_id}/discover-devices` | Queue safe BACnet discovery job | Operator/admin auth |

### 7.2 Admin / Operator Auth

Preferred request format:

```text
Authorization: Bearer <IOT_ADMIN_API_TOKEN>
```

Human operator request format after Supabase login:

```text
Authorization: Bearer <supabase_user_access_token>
```

Requirements:

- Missing admin credentials return HTTP `401`.
- Invalid admin credentials return HTTP `401`.
- Correct Bearer token allows protected endpoints.
- Raw token without `Bearer ` should be rejected unless the contract is explicitly changed.
- Gateway token cannot call operator/admin routes.
- Admin token is for scripts, smoke tests, and emergency automation.
- Admin token must never be printed, committed, logged, or installed on a gateway.
- Supabase Auth owns signup, password storage, login, password reset, and email confirmation.
- The app username is the user's email address.
- Email confirmation does not automatically approve app access.
- Local app role and status decide authorization.

Current app roles:

- `pending`: signed up but not approved.
- `viewer`: read-only gateway/job visibility.
- `operator`: can queue commissioning jobs.
- `admin`: can manage users and gateway provisioning.
- `disabled`: blocked.

### 7.3 Gateway Auth

Gateway-auth routes must remain separate from admin/operator auth.

Gateway token requirements:

- Gateway token may exist only on the edge gateway in `/etc/iot-cx-agent/edge-agent.env`.
- Gateway token is used by the edge agent to authenticate to FastAPI.
- Gateway token must not allow operator/admin actions.
- Gateway token must not allow direct Supabase/Postgres access.

### 7.4 Edge Agent

The edge agent must:

- Call FastAPI only
- Poll for jobs
- Execute jobs locally
- Use local SQLite for edge-side state
- Post job results to FastAPI
- Never connect directly to Supabase/Postgres
- Never use service-role database keys
- Never receive `IOT_ADMIN_API_TOKEN`

### 7.5 BACnet Runtime

BACnet commissioning runtime must:

- Use UDP `47814`
- Use shared lock path `/tmp/iot-cloud-commissioning-bacnet-47814.lock`
- Keep BACnet execution local to the gateway
- Avoid touching legacy UDP `47808`
- Validate BACnet-stack tool availability before executing BACnet jobs

## 8. Non-Functional Requirements

### 8.1 Security

- Least privilege between operator, gateway, and database layers
- No database credentials on edge gateways
- No service-role keys on edge gateways
- No admin/operator tokens on gateways
- Token rotation supported through Render/admin environment
- Strong separation between gateway API token and operator/admin token

### 8.2 Safety

- UDP `47808` legacy runtime must not be modified or used by cloud commissioning jobs.
- BACnet runtime jobs must default to UDP `47814`.
- Commands provided to operators must identify where they run.
- SSH/Linux commands must use one command per copy block.
- Destructive operations require explicit stop and confirmation.

### 8.3 Reliability

- Edge job claim and result posting must tolerate transient cloud connectivity failures.
- Local edge state must survive restart where appropriate.
- Cloud job status must clearly distinguish queued, claimed, completed, failed, and deferred states.

### 8.4 Observability

- Gateway heartbeat timestamps visible in cloud
- Job timestamps visible in cloud
- Job result JSON stored in cloud
- Errors stored without leaking secrets
- Future audit trail for operator actions

### 8.5 Maintainability

- Tests must cover protected routes and gateway-auth routes.
- Documentation must reflect live API contracts.
- OpenAPI/Swagger should expose proper Bearer auth support for protected operator endpoints.
- Project instructions should live in `AGENTS.md` for Codex.

## 9. API Contract Notes

### 9.1 Job Creation

The job creation body uses `request`, not `payload`.

Correct:

```json
{
  "gateway_id": "GW777",
  "job_type": "bacnet_runtime_check",
  "request": {
    "bacnet_port": 47814
  }
}
```

Do not use a job creation field named `payload`; the live API contract expects `request`.

### 9.2 Swagger / OpenAPI

Protected operator endpoints show Bearer authorization in Swagger as `AdminBearer (http, Bearer)`.

Current deployed behavior exposes an OpenAPI `securitySchemes` bearer section, and operator/admin auth works through Swagger Authorize when the `IOT_ADMIN_API_TOKEN` value is entered without the `Bearer` prefix.

## 10. MVP-012 Live Smoke Pass Criteria

The live admin smoke test passes only when:

- `/health` returns OK
- `/health/db` returns OK
- `GET /api/edge/gateways` without token returns `401`
- `GET /api/edge/gateways` with Bearer admin token returns `200`
- Gateway list includes `GW777`
- Gateway list shows `GW777` online
- Gateway list shows `GW777` using BACnet port `47814`
- `POST /api/edge/jobs` with Bearer admin token queues `GW777` `bacnet_runtime_check`
- Queued job uses `request.bacnet_port = 47814`
- `GW777` claims the job
- `GW777` completes the job
- Result confirms BACnet port `47814`
- No admin token is placed on a gateway
- No service-role key is placed on a gateway
- No legacy UDP `47808` behavior is touched

## 11. Roadmap

### MVP-013: Supabase Email Login And Admin User Roles

- Supabase email/password signup.
- Email confirmation before normal login.
- FastAPI Supabase JWT verification.
- Local `operator_users` role and status records.
- Admin user-management API for assigning users.
- Browser pages for login, signup, confirmation handoff, waiting-for-approval, unauthorized access, dashboard, and admin user assignment.
- Existing `IOT_ADMIN_API_TOKEN` retained for scripts and emergency automation.
- The admin users page uses the logged-in Supabase session/JWT instead of manual token paste for normal use.

### MVP-014 Candidate: Edge-Led Commissioning Model

- Operator dashboard links to per-gateway workspaces.
- Gateway status uses heartbeat age, so stale or missing heartbeats are not shown as active just because `latest_status` was previously `online`.
- The cloud UI is the operations platform for fleet, users, jobs, templates, reports, and future graphics/trends.
- The edge UI is the BACnet commissioning workstation for device discovery, point discovery, point selection, local validation, and template export.
- Gateway workspace shows the imported commissioning model: groups, devices, and approved points.
- Completed edge commissioning templates can be imported into the cloud gateway commissioning model.
- Saved devices render under groups with BACnet object-type folders generated from saved point `object_type`.
- Operator/admin users can remove saved devices and points from the default tree by soft-disabling them.
- Cloud-queued BACnet jobs remain available for safe runtime checks and targeted follow-up reads, but cloud should not duplicate the full edge commissioning workstation.
- Viewers can read gateway UI state but cannot create groups, save devices/points, or queue jobs.
- No BACnet write workflow is included.
- Point and device data must come from edge commissioning exports or completed edge-agent BACnet jobs; point data must not be faked.

### MVP-014B Candidate: Edge Template Export And Cloud Import

- Define a versioned commissioning template JSON format for site, gateway, groups, devices, and selected BACnet points.
- Add edge UI export for approved commissioned devices/groups/points.
- Add cloud import preview/apply flow for a gateway.
- Imports are idempotent: existing groups/devices/points are updated or re-enabled; missing ones are created.
- Template import does not require cloud direct BACnet execution.
- Template import does not expose Supabase, Postgres, service-role, admin-token, or server-pepper secrets to the edge gateway.
- First implementation slice: edge saved live devices can download a cloud template JSON, and cloud gateway workspaces can import that JSON into the imported commissioning model.

### MVP-014B Complete: Direct Connect And Site Management

- Cloud UI provides a Configure action for a gateway.
- Cloud separates Cloud Tunnel from Direct Connect.
- Direct Connect is a separate button/link to a configured Cradlepoint/cellular host and port, usually `http://10.x.x.x:5002`.
- Direct Connect opens in a new browser tab and is not a cloud proxy.
- Direct Connect does not store gateway UI passwords in the cloud.
- Direct Connect host/port values are validated server-side; user-controlled schemes and `javascript:` URLs are rejected.
- Site information includes site name, split site address fields (street, city, state, ZIP/postal code), Cradlepoint/direct-connect host, Direct Connect external port, gateway UI internal port, Monday-Friday/Saturday/Sunday store hours, and network status notes.
- Site information currently records that the rest of the boxes on the two known networks are online as well.
- Admin users can edit site information; operator/viewer users are read-only by default.
- The design must not expose admin tokens, gateway tokens, Supabase secrets, service-role keys, server pepper values, or database credentials.
- Direct Connect and Cloud Tunnel do not change cloud BACnet jobs: UDP `47814` remains the cloud commissioning runtime and legacy UDP `47808` remains excluded.
- Live smoke passed: the site info form saves correctly, split address fields work, gateway list/detail display site information correctly, Direct Connect appears after host/port configuration, and Direct Connect opens the forwarded gateway UI through the configured host/port.
- Recommended tag: `mvp-014b-direct-connect-site-management`.

### Future Candidate: Cloud Tunnel Remote Console

- Cloud Tunnel uses the gateway's outbound session to a controlled cloud relay.
- Cloud Tunnel remains separate from Direct Connect.
- When no gateway tunnel client/session is connected, tunnel status remains a friendly disconnected state and the protected proxy route returns `{"detail":"Gateway tunnel is not connected"}`.
- Tunnel connectivity must not be faked.
- Current implementation state is partial: cloud tunnel manager/proxy routes and an edge-agent tunnel client module exist, but live tunnel access requires a provisioned gateway process to run and maintain the outbound WebSocket session.
- Direct browser navigation to `/gateways/{gateway_id}/tunnel/` renders a friendly shell because browser address-bar navigation does not attach the logged-in Supabase bearer token.
- Tunnel proxy access is limited to AdminBearer or active Supabase admin/operator users; viewer users may see status but cannot open the tunnel console.
- Remote console sessions must be audited and expire automatically.
- Cloud Tunnel work must not expose admin tokens, gateway tokens, Supabase secrets, service-role keys, server pepper values, or database credentials.

Recommended tunnel split:

- MVP-Tunnel-A: status-only hardening, friendly disconnected UX, gateway session registration tests, docs, and no gateway service rollout.
- MVP-Tunnel-B: real gateway-initiated WebSocket tunnel client rollout, cloud session manager hardening, browser proxy route, local target allowlist initially limited to the gateway UI on `127.0.0.1:5000`, timeout handling, and tests.
- MVP-Tunnel-C: audit trails, idle/session controls, connection management UX, and operational polish.

### MVP-014C Candidate: BACnet Point Loading And Point-Tree Population

- Implement the real `bacnet_load_points` edge-agent job.
- The edge agent reads BACnet object-list data locally on UDP `47814`.
- The job returns point candidates from the actual gateway/BACnet network; fake point data remains out of scope.
- The cloud UI queues point-load jobs only for eligible saved devices on online gateways.
- The gateway workspace displays completed point-load results and lets an admin/operator save approved point candidates.
- Saved points populate the UI point tree under the saved device with friendly object folders.
- Viewer users remain read-only.
- No BACnet writes, schedules, program edits, command writes, subscriptions, or background polling are included.
- Legacy UDP `47808` remains untouched.

### MVP-014 Later: Commissioning Job Workflows

- BACnet runtime check
- BACnet read
- Job result viewer
- Safe job templates
- CSV/JSON export of results

### MVP-015 Candidate: Gateway Lifecycle Management

- Provision gateway
- Rotate gateway token
- Decommission gateway
- Clone preparation checklist
- Gateway status history

### MVP-016 Candidate: Reporting and Evidence

- Commissioning report generation
- Job evidence bundles
- Site/gateway summary
- Export PDF/CSV/JSON

### MVP-017 Candidate: Role-Based Access Control

- Supabase Auth or equivalent
- User roles
- Site permissions
- Operator audit trail

### MVP-018 Candidate: Realtime Operations

- Realtime gateway status
- Job progress updates
- Notification hooks
- Operator dashboard refresh

## 12. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Admin token mismatch in Render | Operator API unavailable | Verify exact service env var, remove quotes/spaces, redeploy |
| Accidentally using UDP `47808` | Could affect legacy runtime | Hard-code/test commissioning default `47814`; stop on `47808` |
| Edge gateway receives admin token | Security boundary broken | Document and test secret boundaries |
| Edge directly connects to database | Architecture violation | Keep database code out of edge agent |
| Job body uses `payload` instead of `request` | Job validation mismatch | Update docs/tests/scripts |
| Swagger lacks auth support | Operator confusion | Add Bearer security scheme |
| Clone image ships with old identity | Duplicate gateway identity | Clone-safe preparation and validation checklist |

## 13. Acceptance Criteria

The product is acceptable for the current backend phase when:

1. Operator/admin endpoints are protected.
2. Gateway endpoints are protected with gateway credentials.
3. Gateway heartbeat works.
4. Job queueing works.
5. Edge job claim/result flow works.
6. BACnet runtime check executes locally on UDP `47814`.
7. Clone-safe gateway provisioning is documented and proven.
8. Secrets remain in their proper contexts.
9. Legacy UDP `47808` remains untouched.
10. Tests and docs reflect the live API contract.
