# KNOWN_ISSUES — Authoritative Open Issue Register

Machine-readable source: [`authority/project-state.yaml`](../../authority/project-state.yaml) → `readiness.blocking`.

Last updated: 2026-07-29. Each entry carries an explicit verification state.
Nothing here is described as fixed, safe, or working without stated evidence.

---

## Closed by owner determination

### CLOSED-01 — Authoritative Edge Agent 0.1.9 identity

**Resolved 2026-07-29 by Steve.**

```yaml
authoritative_commit: d2722d395ab3b380f8858b5208863a8e49ff2cc3
authoritative_tag: edge-agent-v0.1.9-final
verification:
  state: owner_confirmed_deployment
  deployment_method: legacy_updater
  deployed_device_count: 75
```

> Commit `d2722d395ab3b380f8858b5208863a8e49ff2cc3` is the authoritative Edge
> Agent 0.1.9 release baseline because it is the exact commit referenced by the
> legacy updater and deployed to all 75 online devices.

No further investigation of which commit was the original Agent 0.1.9 release
is required.

---

## Critical

### OPEN-01 — Deployed 0.1.8 source and artifact unrecovered

**State:** `unverified`.
0.1.8 is the declared `rollback_release` for 0.1.9, but no 0.1.8 artifact,
release note, or manifest exists anywhere in the workspace. Tag
`edge-agent-v0.1.8` (`8c81889`) lives only on `origin/codex/edge-release-018`,
never merged to `main`.

**Impact:** the documented rollback path for the deployed fleet is not
reproducible from preserved evidence.
**Constraint:** do not reconstruct a 0.1.8 artifact from an unverified tag and
present it as the deployed one.
**Needed:** owner recollection of what shipped as 0.1.8, plus any off-workspace
artifact or a gateway-local `gw-recovery` checkpoint.

---

## High

### OPEN-02 — Paired Edge UI artifact for the 75-device rollout unconfirmed

**State:** `requires_owner_confirmation`.
At the authoritative agent baseline the manifest paired UI `b4dc654` /
`f7acfbaa…`; `84881fb` re-paired it to `719d4a8` / `a83fc2a6…`. Only `719d4a8`
contains the UI trend gate.

**Impact:** if `b4dc654` reached any device, its trend UI is reachable while the
agent never samples — a UI that appears functional but cannot work.
**Assessment:** `719d4a8` is *likely* the deployed UI, inferred from manifest
lineage (the manifest naming `d2722d3` postdates the re-pairing). Not verified.
**Needed:** owner confirmation, or the UI commit/artifact hash from a gateway.

### OPEN-03 — Maintenance-line identity for `e634e77` undecided

**State:** `unverified` for both approval and deployment.
16 commits after the baseline. Agent payload byte-identical; changes confined to
cloud-api, updater, release tooling, and migrations `0022`/`0023`.

> Later commits on `release/edge-agent-0.1.9`, including `e634e77`, represent
> post-release development or maintenance candidates and do not redefine the
> original 0.1.9 release without separate approval and deployment evidence.

**Constraint:** `e634e77` must not silently redefine release 0.1.9.
**Needed:** decision on whether this becomes `0.1.9.1` or another explicitly
approved version; approval and deployment evidence for the cloud-side changes.

### OPEN-04 — Cloud migrations `0022`/`0023` never validated against the deployed agent

**State:** `unverified`.
Both migrations were added after the authoritative baseline. The combination of
Alembic head `0023_edge_release_targets` with the `d2722d3` agent has no
recorded validation. `release-governance.md` requires additive,
backward-compatible migrations; conformance is unassessed.

**Needed:** read-only `alembic_version` from the live database; assessment of
`0022`/`0023` against the deployed agent's API expectations.

### OPEN-05 — "Rule #1" is enforced but its wording is unstated

**State:** `requires_owner_confirmation`.
`tools/legacy_edge_upgrade_webapp.py` computes `"Rule #1 validation"` and fails
closed unless `verify_manifest_artifact()` and
`validate_embedded_ui_artifact_contents()` both pass. It is reported alongside
`"Background BACnet activity added": "No"` and
`"Local Edge trends default enabled"`. **The rule's text appears in no
repository.**

**Constraint:** do not invent the wording.
**Needed:** Steve's exact statement of Rule #1.

### OPEN-06 — Trend ownership rule unratified

**State:** `requires_owner_confirmation`.
No repository text states the Edge/Cloud trend ownership rule. Searches for
"Edge owns", "Cloud must not configure" return nothing. `docs/SCOPE.md` §3
covers Supabase, token, and BACnet boundaries but is silent on trend ownership.

**Needed:** ratified wording, then representation in code, tests, and
`ENGINEERING_RULES.md`.

### OPEN-07 — Supabase error investigation not started

**State:** `unknown`. 9,000+ dashboard errors observed by Steve; not
categorised. No Supabase contact has been made.

Local evidence: `docs/gw032-trend-backlog-incident.md` records a real
production defect — soft-deleted points left `PointTrendConfig.enabled = true`,
producing 16,692 pending queue rows and repeated 20-second trend-sync timeouts.
Retry backoff is bounded per item (30 s base, 900 s cap) but unbounded in
breadth.

**Contradiction:** `supabase/README.md` states the migrations are *"not
connected to a live Supabase project yet"*, which is contradicted by
`.env.example` (`pooler.supabase.com`), `cloud-api/app/database.py`, and the
GW032 incident's live SQL. That README is obsolete.

**Constraint:** the errors must not be called harmless because the application
appears operational.
**Needed:** authorised read-only error export; project refs for prod vs staging;
whether the GW032 defect was fixed in code or only in data.

### OPEN-08 — Agent version declaration on `main` is `0.1.6`

**State:** `contradicted`.
`main` declares `0.1.6` in `pyproject.toml` and `__init__.py` although 0.1.8 and
0.1.9 shipped. None of the 0.1.8/0.1.9 agent tags are ancestors of `main`.

**Needed:** decision on whether release work merges to `main` and how version
declarations are kept truthful. Not changed here.

### OPEN-09 — Tag `edge-agent-v0.1.9` contradicts its own source

**State:** `contradicted`. Tag `edge-agent-v0.1.9` → `d899d80`, whose source
declares `version = "0.1.8"`. Classified as an invalid release candidate. The
tag has not been moved or rewritten.

---

## Medium

### OPEN-10 — Edge UI has two unrelated histories

**State:** `verified`, unadjudicated. Active line root `491b766`; release-tag
line root `70259c7`; `git merge-base` returns no common ancestor. The 13 Edge UI
trend commits exist only on the archived line.

Both preserved as `archive/edge-ui-pre-recovery-0.1.8` / `-0.1.9` (pushed).
**Needed:** decision on whether the histories are ever reconciled.

### OPEN-11 — Two parallel migration systems

**State:** `verified`. `supabase/migrations/` holds 10 SQL files;
`cloud-api/alembic/versions/` holds 23 Python migrations with a verified linear
chain. No mapping document exists. `supabase/README.md` lists only 5.

### OPEN-12 — `releases/` working copies are mutable and contaminated

**State:** `verified`. Both contain `.venv`, `.pytest_cache`, `__pycache__`;
`releases/iot-cloud-commissioning-0.1.9/test-cloud-api.db` was modified after
checkout. Neither is an immutable artifact. Both are fully represented in their
source repositories (no unique history at risk). Deliberately not cleaned.

### OPEN-13 — `engineering-headquarters` has no approved independent remote

**State:** `verified`. Single commit, no remote. A verified Git bundle exists in
the preservation store but **on the same disk**. Remains a single-machine risk
until copied off-machine or pushed to a remote Steve approves.

---

## Deferred work items (not defects)

- **Restore trends without modifying or degrading `bacrp` and `bacwp`.** Target
  0.2.0. The 0.1.9 disablement is an approved temporary exception, not a defect.
- **Populate or retire `docs/` and `gateway-tools/`** — empty scaffolds, 0
  commits.
- **Reconcile the two divergent copies of `docs/releases/release-index.md`.**
