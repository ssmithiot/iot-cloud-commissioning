import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("GATEWAY_AUTH_PEPPER", "test-pepper")
os.environ.setdefault("IOT_ADMIN_API_TOKEN", "test-admin-token")

from app import access as access_module
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
    # This asserts the REQUIRE_EXPLICIT_MEMBERSHIP=false (default) behavior:
    # the legacy fallback is unchanged. See the flag-on twin test below for
    # the fail-closed behavior once the rollout flag is enabled.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        operator = OperatorUser(email="legacy@example.com", role="viewer", status="active")
        session.add(operator)
        session.commit()

        assert access_module.settings.require_explicit_membership is False
        assert visible_site_ids(
            session,
            AdminAuthContext(auth_type="supabase_user", role="viewer", operator_user_id=str(operator.id)),
        ) is None
    finally:
        session.close()
        engine.dispose()


def test_zero_membership_operator_sees_nothing_when_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_module.settings, "require_explicit_membership", True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        operator = OperatorUser(email="new-customer2-user@example.com", role="operator", status="active")
        session.add(operator)
        session.commit()

        visible = visible_site_ids(
            session,
            AdminAuthContext(auth_type="supabase_user", role="operator", operator_user_id=str(operator.id)),
        )
        assert visible == set()  # fail closed, not the legacy None (unscoped)
    finally:
        session.close()
        engine.dispose()


def test_scoped_operator_visibility_is_unaffected_by_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # The flag only changes the zero-membership case. An operator with real
    # memberships sees exactly the same thing whether the flag is on or off.
    monkeypatch.setattr(access_module.settings, "require_explicit_membership", True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        site = Site(site_id="SITE-SCOPED", name="Scoped site")
        operator = OperatorUser(email="scoped@example.com", role="viewer", status="active")
        session.add_all([site, operator])
        session.flush()
        session.add(SiteMembership(site_uuid=site.id, operator_user_id=operator.id, role="viewer"))
        session.commit()

        visible = visible_site_ids(
            session,
            AdminAuthContext(auth_type="supabase_user", role="viewer", operator_user_id=str(operator.id)),
        )
        assert visible == {str(site.id)}
    finally:
        session.close()
        engine.dispose()


def test_platform_admin_and_admin_token_retain_global_visibility_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_module.settings, "require_explicit_membership", True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        site = Site(id=uuid4(), site_id="SITE-ADMIN-2", name="Admin site 2")
        admin_operator = OperatorUser(email="platform-admin@example.com", role="admin", status="active")
        session.add_all([site, admin_operator])
        session.flush()
        session.commit()

        # Shared admin token: auth_type == "admin_token".
        assert visible_site_ids(session, AdminAuthContext()) is None
        require_site_access(session, AdminAuthContext(), site)

        # An operator whose global role is "admin" -- zero memberships, but
        # is_platform_admin() short-circuits before the fallback is reached.
        admin_auth = AdminAuthContext(auth_type="supabase_user", role="admin", operator_user_id=str(admin_operator.id))
        assert visible_site_ids(session, admin_auth) is None
        require_site_access(session, admin_auth, site)
    finally:
        session.close()
        engine.dispose()
