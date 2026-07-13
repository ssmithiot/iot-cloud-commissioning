# Trend hardening staging validation checklist

Use a new, separate Render service and a separate Supabase project/database. Do not reuse any production service, database URL, gateway credential, or edge configuration.

- [ ] Confirm the staging Render service has its own `CLOUD_DATABASE_URL`, gateway-auth pepper, and admin token, and sets `ENVIRONMENT=staging` (verify via `GET /health`).
- [ ] Apply Alembic to the current head (`0017_gateway_alert_states` on this branch) on the staging database before starting the API; verify `GET /health/schema` reports `status: "ok"` with matching expected/current revisions.
- [ ] Confirm the staging API has `AUTO_CREATE_TABLES=false` and a deliberate `TREND_RETENTION_DAYS` value (90 days by default).
- [ ] Provision a non-production test gateway only. Do not connect a production gateway to staging.
- [ ] Confirm no production tunnel is configured to point at staging, and do not connect a staging tunnel to any production gateway.
- [ ] Configure one or more staging point trends and verify the edge receives only its gateway's enabled trend configuration.
- [ ] Verify a normal batch uploads once, a retry returns the existing samples without duplicates, and a duplicate pair in one batch is rejected.
- [ ] Verify the 500-sample batch ceiling and the UI retrieval limit are rejected when exceeded.
- [ ] Confirm gateway heartbeat output reports pending/deferred trend counts, oldest pending timestamp, and maximum retry attempts.
- [ ] Create a temporary upload failure and verify edge retry backoff and eventual recovery without exceeding the configured local backlog.
- [ ] Verify trend samples display sampled time, received time, source, and quality; confirm samples older than the retention period are pruned by a subsequent successful upload.
- [ ] Record test gateway IDs, timestamps, batch sizes, and results; revoke/delete staging credentials after validation.
