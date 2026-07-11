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

## Next Phase 1 work

Apply the same scope dependency to the remaining tree, point, trend, job-history, and commissioning routes; then expose organization and membership management in the admin UI. Do not infer organization ownership for existing sites automatically.
