# Cloud Schema Governance

## Authority

`cloud-api/alembic/` is the sole schema-migration authority for tables used by the FastAPI application. Its SQLAlchemy models live in `cloud-api/app/models.py`; the container applies migrations with `alembic upgrade head` before starting Uvicorn.

The SQL files under `supabase/migrations/` are not an additional application migration stream. They are historical/planning material for Supabase platform features and must not be independently applied to create or alter FastAPI-owned tables. Any future Supabase-managed capability must either:

1. receive an Alembic migration when the FastAPI application owns its data; or
2. be explicitly isolated, documented, and given a non-overlapping table/schema ownership boundary.

Do not add the same entity to both migration trees.

## Deployment contract

Production and shared environments must set:

```text
AUTO_CREATE_TABLES=false
```

At application startup, the API reads `alembic_version` and compares it with the Alembic head bundled in that deployed source tree. Startup fails if they differ. This prevents the API from silently creating tables or adding columns as a substitute for a real migration.

`AUTO_CREATE_TABLES=true` is reserved for isolated local development and test databases. It is not a migration strategy and does not make a development database eligible for production deployment.

## Operator verification

From the same environment and `CLOUD_DATABASE_URL` used by the API:

```bash
cd cloud-api
alembic heads
alembic current
alembic upgrade head
```

After deployment, verify:

```text
GET /health
GET /health/db
GET /health/schema
```

`/health/schema` reports the bundled expected revision(s), the database revision(s), and `migration_authority: "alembic"`. It must report `status: "ok"` in managed environments.

## Change procedure

1. Create and review one Alembic migration for the application schema change.
2. Update SQLAlchemy models and API tests in the same change.
3. Run upgrade and downgrade checks against a disposable PostgreSQL database.
4. Deploy with `alembic upgrade head`; confirm `/health/schema` is current.
5. Update this document if a new schema owner or platform boundary is introduced.

Never restore a missing production column with application-startup DDL. Add a migration, apply it, and let the revision gate prove the result.
