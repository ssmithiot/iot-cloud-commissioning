from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Explicit deployment identity. Exposed (value only, never secrets) on
    # /health so humans and tooling can verify which environment they are
    # talking to before acting on it.
    environment: Literal["development", "staging", "production"] = Field(
        default="development", validation_alias="ENVIRONMENT"
    )
    # Escape hatch for the staging safety guard below. Leave false in staging.
    allow_production_resources: bool = Field(default=False, validation_alias="ALLOW_PRODUCTION_RESOURCES")
    # Comma-separated substrings that identify production resources (hosts,
    # project refs). Used only when environment=staging to refuse accidental
    # production configuration. Extend with the production Supabase project
    # ref once known. Name-based detection is inherently incomplete; this
    # guard catches known values, it does not prove isolation.
    production_resource_fingerprints: str = Field(
        default="iot-cloud-api-dev.onrender.com",
        validation_alias="PRODUCTION_RESOURCE_FINGERPRINTS",
    )
    # Tenant isolation rollout flag (Customer 2 prep). While false (default),
    # an active operator/viewer with zero organization and zero site
    # memberships keeps the legacy fallback in app.access.visible_site_ids:
    # full visibility, for backward compatibility with pre-membership
    # accounts. Flip to true only after Customer 1's membership backfill is
    # verified complete (every active operator/viewer has an explicit
    # membership) -- then the same zero-membership case sees nothing instead
    # of everything (fail closed, not fail open). Never affects
    # role=="admin" operators or the admin token, which stay globally scoped
    # by design either way. See docs/technical-debt-register.md Tier 2 #9.
    require_explicit_membership: bool = Field(default=False, validation_alias="REQUIRE_EXPLICIT_MEMBERSHIP")
    database_url: str = Field(
        default="sqlite:///./cloud-api-dev.db",
        validation_alias=AliasChoices("DATABASE_URL", "CLOUD_DATABASE_URL"),
    )
    auto_create_tables: bool = Field(
        default=False,
        validation_alias=AliasChoices("AUTO_CREATE_TABLES", "CLOUD_AUTO_CREATE_TABLES"),
    )
    gateway_auth_pepper: str = Field(min_length=1, validation_alias="GATEWAY_AUTH_PEPPER")
    admin_api_token: str = Field(min_length=1, validation_alias="IOT_ADMIN_API_TOKEN")
    supabase_jwt_secret: str | None = Field(default=None, validation_alias="SUPABASE_JWT_SECRET")
    supabase_jwt_audience: str = Field(default="authenticated", validation_alias="SUPABASE_JWT_AUDIENCE")
    supabase_url: str | None = Field(default=None, validation_alias="SUPABASE_URL")
    supabase_anon_key: str | None = Field(default=None, validation_alias="SUPABASE_ANON_KEY")
    # Server-only key for Supabase Auth admin actions such as invitations.
    # Never expose this key in browser code, API responses, or edge agents.
    supabase_service_role_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"),
    )
    # Optional exact allowed redirect URL for Supabase invitation links. When
    # unset, the invite endpoint uses the current application origin.
    supabase_invite_redirect_url: str | None = Field(default=None, validation_alias="SUPABASE_INVITE_REDIRECT_URL")
    supabase_jwks_url: str | None = Field(default=None, validation_alias="SUPABASE_JWKS_URL")
    gateway_stale_after_seconds: int = Field(default=300, validation_alias="GATEWAY_STALE_AFTER_SECONDS")
    gateway_offline_after_seconds: int = Field(default=1800, validation_alias="GATEWAY_OFFLINE_AFTER_SECONDS")
    trend_retention_days: int = Field(default=90, ge=1, le=3650, validation_alias="TREND_RETENTION_DAYS")
    heartbeat_retention_days: int = Field(default=30, ge=1, le=3650, validation_alias="HEARTBEAT_RETENTION_DAYS")
    # Database connection pool controls. Applied only to non-SQLite URLs.
    # Defaults match SQLAlchemy's QueuePool defaults except pool_recycle,
    # which is set below typical managed-Postgres idle timeouts. Actual
    # Supabase/Render connection limits must be confirmed per selected plan
    # before raising pool sizes.
    db_pool_size: int = Field(default=5, ge=1, le=100, validation_alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, ge=0, le=100, validation_alias="DB_MAX_OVERFLOW")
    db_pool_timeout_sec: int = Field(default=30, ge=1, le=600, validation_alias="DB_POOL_TIMEOUT")
    db_pool_recycle_sec: int = Field(default=1800, ge=60, le=86400, validation_alias="DB_POOL_RECYCLE")
    # Optional admin-only shortcut to this environment's Render metrics page.
    # The cloud app never receives a Render API key or attempts to proxy its
    # private metrics; it links operators to the Render dashboard instead.
    render_metrics_url: str | None = Field(default=None, validation_alias="RENDER_METRICS_URL")
    # Fleet alerting groundwork. With no webhook URL configured, evaluation
    # runs silently: transitions are tracked and reported in the endpoint
    # response but nothing is delivered. Paste a Slack/Teams-compatible
    # incoming-webhook URL to go live without a deploy.
    alert_webhook_url: str | None = Field(default=None, validation_alias="ALERT_WEBHOOK_URL")
    # A gateway online with its oldest pending trend sample older than this
    # is failing to drain its upload backlog.
    alert_trend_backlog_age_hours: int = Field(default=6, ge=1, le=720, validation_alias="ALERT_TREND_BACKLOG_AGE_HOURS")
    # Ceiling on webhook deliveries per evaluation run (protects against a
    # fleet-wide event storming the webhook endpoint).
    alert_max_deliveries_per_run: int = Field(default=50, ge=1, le=500, validation_alias="ALERT_MAX_DELIVERIES_PER_RUN")
    # Jobs claimed longer than this without a result are considered stale and
    # requeued at the owning gateway's next poll. BACnet write jobs are
    # excluded from requeue and remain visible for manual review.
    job_claim_timeout_sec: int = Field(default=600, ge=30, le=86400, validation_alias="JOB_CLAIM_TIMEOUT_SEC")
    # Minimum seconds between auth telemetry writes (gateway credential
    # last_used_at, operator last_login_at). Avoids one UPDATE+COMMIT per
    # authenticated request at fleet scale. 0 restores write-every-request.
    auth_telemetry_min_interval_sec: int = Field(default=60, ge=0, le=86400, validation_alias="AUTH_TELEMETRY_MIN_INTERVAL_SEC")
    # Tunnel fallback is for slow remote gateway pages/actions. Keep the
    # request timeout long enough for field operations; session TTL remains a
    # separate control enforced by TunnelSessionManager.
    tunnel_request_timeout_sec: float = Field(default=900.0, validation_alias="TUNNEL_REQUEST_TIMEOUT_SEC")

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        extra="ignore",
    )


settings = Settings()


def production_resource_conflicts(active_settings: Settings) -> list[str]:
    """Return names (never values) of settings that match known production
    resource fingerprints while running as staging.

    Only enforced for environment=staging with ALLOW_PRODUCTION_RESOURCES
    unset/false. Detection is fingerprint-based and therefore incomplete by
    design: it prevents the known-value mistakes, not all mistakes. True
    isolation still requires the staging setup checklist.
    """
    if active_settings.environment != "staging" or active_settings.allow_production_resources:
        return []
    fingerprints = [
        fragment.strip().lower()
        for fragment in active_settings.production_resource_fingerprints.split(",")
        if fragment.strip()
    ]
    if not fingerprints:
        return []
    candidates = {
        "DATABASE_URL": active_settings.database_url,
        "SUPABASE_URL": active_settings.supabase_url or "",
        "SUPABASE_JWKS_URL": active_settings.supabase_jwks_url or "",
    }
    return sorted(
        name
        for name, value in candidates.items()
        if any(fragment in value.lower() for fragment in fingerprints)
    )
