"""Application-owned organization and site access helpers."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AdminAuthContext
from app.config import settings
from app.models import OrganizationMembership, Site, SiteMembership


def is_platform_admin(auth: AdminAuthContext) -> bool:
    return auth.auth_type == "admin_token" or auth.role == "admin"


def visible_site_ids(db: Session, auth: AdminAuthContext) -> set[str] | None:
    """Return visible Site UUIDs, or None for the existing platform-admin scope."""
    if is_platform_admin(auth):
        return None
    if auth.operator_user_id is None:
        return set()

    direct_site_ids = set(
        str(site_id)
        for site_id in db.scalars(
            select(SiteMembership.site_uuid).where(SiteMembership.operator_user_id == auth.operator_user_id)
        )
    )
    organization_ids = list(
        db.scalars(
            select(OrganizationMembership.organization_id).where(
                OrganizationMembership.operator_user_id == auth.operator_user_id
            )
        )
    )
    if organization_ids:
        direct_site_ids.update(
            str(site_id)
            for site_id in db.scalars(select(Site.id).where(Site.organization_id.in_(organization_ids)))
        )
    # Existing active operators predate memberships. While
    # REQUIRE_EXPLICIT_MEMBERSHIP is false (default), keep their current
    # access until an administrator assigns their first scope -- this is the
    # legacy fallback, unchanged. Once the flag is true (after Customer 1's
    # membership backfill is verified complete), a zero-membership
    # operator/viewer sees nothing instead of everything: fail closed, not
    # fail open. Platform admins are unaffected either way -- handled above,
    # before this branch is ever reached.
    if not direct_site_ids and not organization_ids:
        if settings.require_explicit_membership:
            return set()
        return None
    return direct_site_ids


def require_site_access(db: Session, auth: AdminAuthContext, site: Site) -> None:
    allowed = visible_site_ids(db, auth)
    if allowed is not None and str(site.id) not in allowed:
        # Use 404 so a scoped operator cannot enumerate unrelated sites.
        raise HTTPException(status_code=404, detail="Site not found")
