# RELEASES — Authoritative Release Record

Machine-readable source: [`authority/releases/0.1.9.yaml`](../../authority/releases/0.1.9.yaml),
[`authority/project-state.yaml`](../../authority/project-state.yaml).
Where this document and those files disagree, the YAML is authoritative for
release identity and this document is authoritative for reasoning.

Last updated: 2026-07-29.

---

## Edge Agent 0.1.9 — DEPLOYED (authoritative)

```yaml
release: 0.1.9
component: edge-agent
status: deployed

authoritative_commit: d2722d395ab3b380f8858b5208863a8e49ff2cc3
authoritative_tag: edge-agent-v0.1.9-final

verification:
  state: owner_confirmed_deployment
  deployment_method: legacy_updater
  deployed_device_count: 75

trend_status:
  enabled: false
  decision: approved_temporary_exception
  permanent_architecture: false

maintenance_line:
  candidate_head: e634e77
  approval_status: unverified
  deployment_status: unverified
```

> Commit `d2722d395ab3b380f8858b5208863a8e49ff2cc3` is the authoritative Edge
> Agent 0.1.9 release baseline because it is the exact commit referenced by the
> legacy updater and deployed to all 75 online devices.

> Later commits on `release/edge-agent-0.1.9`, including `e634e77`, represent
> post-release development or maintenance candidates and do not redefine the
> original 0.1.9 release without separate approval and deployment evidence.

### Basis of determination

Owner determination by Steve, 2026-07-29:

1. The legacy updater contains this exact Git reference.
2. The legacy updater was used to push this code to all 75 online devices.
3. This was the code Steve approved for the 0.1.9 rollout.
4. It contains the temporarily approved trends-disabled behavior.
5. It therefore represents the actual approved and deployed 0.1.9 Agent release.

This is **not** a candidate requiring runtime confirmation. It is recorded as
`owner_confirmed_deployment`.

### Repository corroboration

| Evidence | Value |
|---|---|
| `tools/releases/manifests/edge-0.1.9.json` → `agent_source_commit` | `d2722d395ab3b380f8858b5208863a8e49ff2cc3` |
| `tools/tests/test_legacy_edge_upgrade_webapp.py` → `FINAL_AGENT_COMMIT` | `d2722d395ab3b380f8858b5208863a8e49ff2cc3` (test-enforced) |
| Tag `edge-agent-v0.1.9-final` | resolves to `d2722d3` |
| `edge-agent/pyproject.toml` at `d2722d3` | `version = "0.1.9"` |
| Trend default at `d2722d3` | `local_edge_trends_enabled: bool = False` |

The manifest is the one the legacy updater loads as `DEFAULT_RELEASE_MANIFEST`,
so the updater's own release definition names this commit as the agent source.

---

## Release-identity classifications

| Commit | Classification | Authoritative? |
|---|---|---|
| `d2722d395ab3b380f8858b5208863a8e49ff2cc3` | **Authoritative Edge Agent 0.1.9 release baseline** | **Yes** |
| `e634e77` | Later 0.1.9 development or maintenance-line head | No |
| `d899d80` | Contradicted or invalid 0.1.9 release candidate | No |
| `71656b4` | Stale mutable release working copy | No |

### `e634e77` — Later 0.1.9 development or maintenance-line head

Must **not** be described as the original deployed 0.1.9 release.

Determined so far (repository evidence, read-only):

- **What changed after `d2722d3`:** 16 commits, a linear descendant line.
  Changes are confined to `cloud-api/` (main, ui, tunnel, config, models,
  schemas, tests), `tools/` (legacy updater, release manifest tooling and
  tests), Alembic migrations `0022_edge_local_trend_samples` and
  `0023_edge_release_targets`, `docs/releases/edge/0.1.9.md`, and the UI
  release artifact. Total 16 files, +1463 / −158.
- **Agent payload impact: none.** The `edge-agent/` tree is byte-identical at
  both commits (`fdea438fcf435bce6b4128697d3401a295c0b7da`). No post-release
  commit altered the agent code shipped to the fleet.
- **Whether those changes were approved:** `unverified`.
- **Whether any were deployed:** `unverified`. The cloud-API changes would take
  effect through a Render deployment, not the gateway updater, and no
  deployment evidence has been collected.
- **Compatibility with the authoritative release:** for the agent component,
  identical and therefore compatible. Cloud-side compatibility is
  `unverified` pending the migration/deployment questions below.
- **Version identity:** undecided. If these changes are to ship, they require an
  explicitly approved identity such as `0.1.9.1`, not a silent redefinition
  of 0.1.9.

Until those questions are resolved, `e634e77` must not silently redefine
release 0.1.9.

### `d899d80` — Contradicted or invalid 0.1.9 release candidate

- The tag is named `edge-agent-v0.1.9`.
- The source declares `version = "0.1.8"` in both `pyproject.toml` and
  `__init__.py`.
- It was not the commit used by the legacy updater for the 75-device rollout.
- It is not an ancestor of `edge-agent-v0.1.9-final`.

Not authoritative. The tag remains in place as historical evidence and has not
been moved or rewritten.

### `71656b4` — Stale mutable release working copy

Located at `releases/iot-cloud-commissioning-0.1.9`. It is not authoritative
merely because it exists under `releases/`. It is a mutable Git working copy
containing a `.venv`, `.pytest_cache`, `__pycache__`, and a `test-cloud-api.db`
modified after checkout, and it is 7 commits behind the maintenance-line head.

---

## Paired Edge UI artifact — REQUIRES OWNER CONFIRMATION

The owner determination fixes the **Edge Agent** commit. The Edge UI artifact
paired with 0.1.9 changed after `d2722d3` and is a separate question.

| | At `d2722d3` (authoritative agent baseline) | Current release line (`e634e77`) |
|---|---|---|
| `edge_ui_tag` | `b4dc654793af17a2a440baa5142b8eee07e08880` | `719d4a82ed972269d44db7c0638800b26e82002d` |
| `sha256` | `f7acfbaad0d83a63c5b6fac2db80cae296ae07fe332d2d660dc92480dbe1a475` | `a83fc2a6c3f17d188f8fbed13352e07a924f7c619869950a0e140890f710f683` |
| Artifact size | 162,991 bytes | 163,585 bytes |
| UI trend gate `EDGE_TRENDS_UI_ENABLED` | **absent** | **present** |
| Recoverable | Yes (git blob `a9c1842b7f13646a1d17e1ebeaed20173b01ffe5`) | Yes (on disk, hash verified) |

Commit `84881fb` ("Rebuild 0.1.9 UI release artifact") re-paired the artifact;
commit `d679871` ("Align 0.1.9 release definition") then added
`agent_source_commit` and `local_edge_trends_default_enabled` to the manifest.

**Assessment — likely, not verified.** Because the manifest that names
`d2722d3` first exists at `d679871`, which is *after* the artifact was
re-paired, an updater build able to cite `d2722d3` also carried UI `719d4a8`
/ `a83fc2a6`. That is inference from manifest lineage, not deployment evidence.

**Open question for Steve:** which Edge UI artifact reached the 75 devices? This
matters because `b4dc654` has no UI trend gate, so an agent-disabled /
UI-enabled pairing would display a trend UI that never receives samples.

---

## Edge Agent 0.1.8 — UNVERIFIED

| Field | Value |
|---|---|
| Candidate tag | `edge-agent-v0.1.8` → `8c81889` |
| Declared version at tag | `0.1.8` (consistent) |
| Branch | `origin/codex/edge-release-018` only — never merged to `main` |
| Artifact | **none anywhere in the workspace** |
| Release note | **none** (`docs/releases/edge/` has 0.1.6, 0.1.7, 0.1.9) |
| Manifest | **none** (`tools/releases/manifests/` has 0.1.7, 0.1.9) |
| Verification | `unverified` |

0.1.8 is the declared `rollback_release` for 0.1.9 and the declared rollback
target in `docs/releases/release-index.md`. **The documented rollback path has
no preserved artifact.** Recovering the exact deployed 0.1.8 source and artifact
is an open work item. No 0.1.8 artifact may be reconstructed from an unverified
tag and presented as the deployed one.

---

## Release ledger status

`docs/releases/release-index.md` currently lists Edge 0.1.6, 0.1.7, 0.1.9 and
Cloud 2026.07.18.1. It does not list 0.1.8. Two divergent copies of the ledger
exist — one on `main`, one on `release/edge-agent-0.1.9` — and they differ.
Reconciling them is pending and must not be done by overwriting either copy.
