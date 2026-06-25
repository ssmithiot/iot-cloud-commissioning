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
```

Current MVP-013 direction:

```text
identity provider: Supabase Auth
username: email address
email confirmation: Supabase confirmation email
app roles: operator_users table
automation auth: IOT_ADMIN_API_TOKEN remains server-side only
browser pages: /login, /signup, /app, /admin/users
```

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
