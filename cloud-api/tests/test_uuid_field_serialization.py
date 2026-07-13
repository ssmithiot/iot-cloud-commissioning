"""Regression tests: UUID-backed ORM attributes in str-typed response fields.

On PostgreSQL, CloudUUID columns return uuid.UUID objects. Pydantic v2 does
not coerce UUID -> str, so response schemas validated from ORM attributes
failed with a 500 for any site assigned to an organization
(2026-07-13, GW006 / DEV-CLONE-MASTER `/api/ui/gateways/{id}/site` incident).

SQLite test databases store these columns as String(36) and return plain
strings, so endpoint-level tests cannot reproduce the defect; these tests
exercise the schema layer directly with uuid.UUID inputs.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.schemas import OrganizationOut, SiteOut


def _site_row(organization_id: object) -> SimpleNamespace:
    return SimpleNamespace(
        site_id="DEV-CLONE-MASTER",
        name="Dev Clone Master",
        external_ip=None,
        address=None,
        store_hours_mf=None,
        store_hours_sat=None,
        store_hours_sun=None,
        organization_id=organization_id,
    )


def test_site_out_coerces_uuid_organization_id() -> None:
    org_uuid = uuid4()
    out = SiteOut.model_validate(_site_row(org_uuid))
    assert out.organization_id == str(org_uuid)
    assert isinstance(out.organization_id, str)


def test_site_out_accepts_string_organization_id() -> None:
    out = SiteOut.model_validate(_site_row("dd365c33-90a8-43ec-970d-136eca4607db"))
    assert out.organization_id == "dd365c33-90a8-43ec-970d-136eca4607db"


def test_site_out_accepts_null_organization_id() -> None:
    out = SiteOut.model_validate(_site_row(None))
    assert out.organization_id is None


def test_organization_out_coerces_uuid_id() -> None:
    org_uuid = uuid4()
    row = SimpleNamespace(
        id=org_uuid,
        name="The Internet of Team",
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    out = OrganizationOut.model_validate(row)
    assert out.id == str(org_uuid)
    assert isinstance(out.id, str)
    # Round-trips back to the same UUID value.
    assert UUID(out.id) == org_uuid
