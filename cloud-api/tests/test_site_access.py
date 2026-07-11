import os
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("GATEWAY_AUTH_PEPPER", "test-pepper")
os.environ.setdefault("IOT_ADMIN_API_TOKEN", "test-admin-token")

from app.access import require_site_access, visible_site_ids
from app.auth import AdminAuthContext
from app.database import Base
from app.models import Organization, OrganizationMembership, OperatorUser, Site, SiteMembership


def test_operator_visibility_combines_organization_and_direct_site_memberships() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        organization = Organization(name="North Region")
        organization_site = Site(site_id="SITE-ORG", name="Organization site", organization=organization)
        direct_site = Site(site_id="SITE-DIRECT", name="Direct site")
        hidden_site = Site(site_id="SITE-HIDDEN", name="Hidden site")
        operator = OperatorUser(email="viewer@example.com", role="viewer", status="active")
        session.add_all([organization, organization_site, direct_site, hidden_site, operator])
        session.flush()
        session.add(OrganizationMembership(organization_id=organization.id, operator_user_id=operator.id, role="viewer"))
        session.add(SiteMembership(site_uuid=direct_site.id, operator_user_id=operator.id, role="viewer"))
        session.commit()

        auth = AdminAuthContext(auth_type="supabase_user", role="viewer", operator_user_id=str(operator.id))
        visible = visible_site_ids(session, auth)

        assert visible == {str(organization_site.id), str(direct_site.id)}
        require_site_access(session, auth, organization_site)
        require_site_access(session, auth, direct_site)
        try:
            require_site_access(session, auth, hidden_site)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
        else:
            raise AssertionError("Hidden site should not be visible to the scoped operator")
    finally:
        session.close()
        engine.dispose()


def test_platform_admin_retains_global_site_visibility() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        site = Site(id=uuid4(), site_id="SITE-ADMIN", name="Admin site")
        session.add(site)
        session.commit()

        assert visible_site_ids(session, AdminAuthContext()) is None
        require_site_access(session, AdminAuthContext(), site)
    finally:
        session.close()
        engine.dispose()


def test_unscoped_legacy_operator_keeps_existing_visibility_until_assigned() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        operator = OperatorUser(email="legacy@example.com", role="viewer", status="active")
        session.add(operator)
        session.commit()

        assert visible_site_ids(
            session,
            AdminAuthContext(auth_type="supabase_user", role="viewer", operator_user_id=str(operator.id)),
        ) is None
    finally:
        session.close()
        engine.dispose()
