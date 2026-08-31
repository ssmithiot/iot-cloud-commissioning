# Release Governance and Recovery

## Source of truth

Production source is a Git tag, never a local folder.  Edge UI source and
artifacts are recorded in `ssmithiot/edge-bacnet-commissioning-ui`; cloud and
agent source, deployment commits, and this release ledger are recorded in
`ssmithiot/iot-cloud-commissioning`.

Each release note in `docs/releases/edge/` or `docs/releases/cloud/` states its
base release, immutable tag/commit, SHA-256 artifact checksum, component scope,
validation result, gateway-data preservation statement, and rollback target.

## Cloud source authority

`origin/main` is the sole persistent Cloud source authority. Development
branches are temporary validation workspaces only; they are not release
authorities or future development baselines.

All new Cloud development must begin with:

```bash
git fetch origin
git switch main
git pull --ff-only origin main
```

Then create a new development branch from `main`. Before modifying code, its
starting SHA must equal the current `origin/main` SHA. If it does not, the
**AUTHORITY GATE FAILED** and development must stop.

Allowed Cloud flow:

```text
origin/main → temporary development branch → code/test → DEV deployment →
live validation → integrate validated result into main → push origin/main
```

Do not branch from another `codex/*` branch, use a historical rollout branch
as a baseline, allow Render branch selection to redefine source authority, or
silently continue from stale source.

### Current rollout configuration

The following are Render runtime settings, not Cloud source defaults:

- `EDGE_RELEASE_VERSION=0.2.4` — rollout/update-needed label only.
- Edge UI authority: `141de85c5cc6baae045778e61f621fba5e398ef6`.
- Edge Agent authority: `d9232758b93fc9954a64235be918724df08238de`.
- Agent Python/module version: `0.2.3`.

The Cloud rollout label is not the Agent Python/module version.

## Storage

| Purpose | Location | Retention |
| --- | --- | --- |
| Source and tags | GitHub repositories above | Permanent |
| Release ledger | `docs/releases/` in this repository | Permanent |
| Release manifest/checksum | `tools/releases/manifests/` | Permanent |
| Deployable code artifact | GitHub Release asset; updater cache `tools/releases/` | Permanent |
| Gateway code checkpoint | `/home/swadmin/gw-recovery/<release>/` | Keep all validated checkpoints |
| Full gateway-state checkpoint | `/home/swadmin/gw-recovery/<release>/full-state/` | Only when runtime data changes |

Release artifacts never include `.env`, `data/`, gateway identity, IP settings,
credentials, or `start.sh`.  Those remain gateway-local.

## Required release gates

1. Record the exact production base tag and cloud commit before writing code.
2. Work in a clean worktree created from that base.
3. Create a release manifest and validate the artifact checksum.
4. Run component tests and record their commands/results in the release note.
5. Create a GW006 pre-update code checkpoint.
6. Validate GW006, then a 2--3 gateway batch, then the fleet.
7. Publish final tags/assets and mark the release `Production` only after the
   fleet result is recorded.

Any BACnet read/write, RPM, scheduling, migration, updater, or gateway-data
change is high risk and may not skip GW006 validation.

## Rollback rules

### Edge code rollback

Use the updater's **code-only restore**.  It stops the UI, moves the failed
code aside, restores only code/templates/requirements, then starts and checks
the service.  It must preserve `data/`, `.env`, `start.sh`, credentials, and
network/site data.

### Full-state rollback

Use only when a release intentionally changed gateway runtime data (for
example, saved Control Basic program data).  It requires an explicit warning
and restores a named full-state checkpoint.

### Cloud rollback

Manually deploy the previous immutable Render commit/tag.  Normal cloud
rollback is code-only and never automatically downgrades a database.  Database
migrations must be additive and backward-compatible; a destructive migration
requires a separately approved backup and reversal procedure.

## Stop conditions

Stop a rollout and restore affected gateways on any unexpected BACnet read,
write, RPM, authentication, gateway-data, or health regression.  Do not patch
forward during a stopped rollout.  Record the incident and resume only from a
newly validated release candidate.
