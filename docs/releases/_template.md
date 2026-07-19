# <Edge or Cloud> Release <version>

Status: Candidate | Pilot | Production | Rolled back

Base release/commit: `<exact immutable tag or commit>`

## Scope

- Edge UI: changed | unchanged
- Edge agent: changed | unchanged
- Cloud: changed | unchanged
- Database migration: none | additive | destructive
- Gateway/site data: preserved | explicitly changed

## Immutable source

- Source tag/commit: `<value>`
- Artifact: `<GitHub Release asset or local path>`
- SHA-256: `<value>`
- Manifest: `<path>`

## Validation

- Tests: `<commands and results>`
- GW006 checkpoint: `<path/name>`
- GW006 result: `<result>`
- Batch/fleet result: `<result>`

## Rollback

- Edge: `<named code-only checkpoint/release>`
- Cloud: `<Render commit/tag>`
- Data migration action: `<none or approved procedure>`
