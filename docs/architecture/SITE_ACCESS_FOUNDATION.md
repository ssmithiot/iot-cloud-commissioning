# Site Access Foundation

Phase 1 introduces application-owned scope records for the FastAPI cloud database:

- `organization_memberships` grants an operator access to every site owned by an organization.
- `site_memberships` grants an operator direct access to one site.

The existing `operator_users.role` still controls what an authenticated user may do (`viewer`, `operator`, or platform `admin`). Memberships control which sites a non-admin can see. Admin-token access and existing application `admin` users retain global scope for operational continuity.

## Staged rollout

Existing active operators predate membership records. They retain legacy global visibility until an administrator assigns their first organization or direct-site membership. After that first assignment, site scope is enforced for core site and gateway routes. This allows tenant onboarding without abruptly locking out current field workflows.

## Administrative API

All endpoints below require a platform admin or the admin token.

```text
POST /api/admin/organizations
GET  /api/admin/organizations
PUT  /api/admin/organizations/{organization_id}/members
PUT  /api/admin/sites/{site_id}/organization/{organization_id}
PUT  /api/admin/sites/{site_id}/members
```

Membership requests use:

```json
{
  "email": "operator@example.com",
  "role": "viewer"
}
```

The operator must already exist in `operator_users`. The membership role is stored for the forthcoming site-level capability rules; current mutation authority remains governed by the existing application role.

## Completed Phase 1 scope

- Site scope is enforced for workspace trees, groups, devices, point reads and edits, trend configuration and history, discovery, template import, and operator job history.
- The Cloud admin page can create organizations, assign a site to an organization, and grant an existing operator a direct-site or organization-wide scope.
- Existing sites remain unassigned until an administrator deliberately assigns them; no ownership was inferred from legacy data.

## Remaining product work outside this foundation

Membership removal and richer capability rules can be added when the operator workflow requires them. Current scope roles are recorded for that purpose; platform `admin` remains the authority for access administration.
