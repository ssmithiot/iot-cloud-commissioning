# IOT Cloud Commissioning Documentation Bundle

This bundle contains the core Markdown documents for the IOT Cloud Commissioning project:

1. `PRD.md` - product requirements document
2. `ERD.md` - entity relationship document with Mermaid diagrams
3. `SCOPE.md` - full project scope and milestone breakdown

Current deployed state reflected by this bundle:

```text
deployed commit: 11c8b1f
live API: https://iot-cloud-api-dev.onrender.com
admin auth: AdminBearer (http, Bearer)
job creation body field: request
runtime-check request: { "bacnet_port": 47814 }
latest smoke: Direct Connect / Site Info passed
```

Current MVP-013 direction:

```text
identity provider: Supabase Auth
username: email address
email confirmation: Supabase confirmation email
app roles: operator_users table
automation auth: IOT_ADMIN_API_TOKEN remains server-side only
browser pages: /login, /signup, /app, /admin/users
JWT verification: HS256 secret or Supabase JWKS signing keys
confirmation redirect: ${window.location.origin}/login
```

Current MVP-014 direction:

```text
operator dashboard: /app
gateway workspace: /gateways/{gateway_id}
gateway UI API prefix: /api/ui
effective gateway status: heartbeat-age derived online/stale/offline
cloud role: fleet, users, jobs, templates, reports, future graphics/trends
edge UI role: BACnet commissioning workstation for discovery, point selection, validation
remote console bridge: future controlled launcher for cloud-authenticated edge UI access
direct connect: optional new-tab link to configured Cradlepoint/cellular host on port 5002
site info: name, split address (street/city/state/ZIP), direct-connect host/ports, M-F/Sat/Sun store hours, network status notes
network status note: rest of the boxes on the two known networks are online as well
direct connect smoke: passed; opens forwarded gateway UI through configured host/port
cloud tunnel status: separate from Direct Connect; friendly disconnected state remains valid when no gateway tunnel session is connected
recommended slice tag: mvp-014b-direct-connect-site-management
next slice: MVP-014C real bacnet_load_points edge-agent job plus UI point-tree population
template flow: edge builds approved devices/groups/points, cloud imports the template
imported commissioning model: cloud stores imported gateway groups, BACnet devices, BACnet points
edge export: /devices/export/{device_profile_id}.json from the edge UI
cloud import: POST /api/ui/gateways/{gateway_id}/commissioning-template/import
remove behavior: soft-disable saved devices/points; preserve history
viewer role: read-only UI state
operator role: can view site info and Direct Connect when configured
admin role: can edit site info and Direct Connect metadata
BACnet writes: out of scope
fake point data: out of scope
```

Cloud Tunnel remains future scope unless a real gateway tunnel client/session is safely completed. Do not represent Direct Connect as tunnel connectivity and do not fake tunnel status.

Current live smoke handoff:

```text
GW777 visible through authenticated GET /api/edge/gateways
GW777 status: online
GW777 bacnet_port: 47814
latest smoke job: job-3dcf32e743414f37be81d50d447a565b
latest smoke job status: queued
latest smoke job request JSON: { "bacnet_port": 47814 }
```

Automated smoke helper:

```text
scripts/live_mvp012_smoke.py
```

The script reads `IOT_CLOUD_API_URL` or defaults to the live dev API, reads `IOT_ADMIN_API_TOKEN` from the process environment, queues only a safe `GW777` `bacnet_runtime_check` with `{ "bacnet_port": 47814 }`, and exits nonzero if the job fails or does not complete before timeout.

These documents are written for Codex-assisted engineering. They preserve the main architecture rules:

- Edge agent calls FastAPI only.
- Edge gateways never connect directly to Supabase/Postgres.
- Edge gateways never receive admin/operator tokens.
- Cloud commissioning BACnet runtime uses UDP `47814`.
- Legacy UDP `47808` must not be touched.
