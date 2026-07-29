# COMPATIBILITY — Authoritative Compatibility Record

Machine-readable source: [`authority/project-state.yaml`](../../authority/project-state.yaml),
[`authority/releases/0.1.9.yaml`](../../authority/releases/0.1.9.yaml).

Last updated: 2026-07-29.

---

## 1. Edge Agent 0.1.9 — deployed baseline

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
```

> Commit `d2722d395ab3b380f8858b5208863a8e49ff2cc3` is the authoritative Edge
> Agent 0.1.9 release baseline because it is the exact commit referenced by the
> legacy updater and deployed to all 75 online devices.

Fleet baseline for compatibility purposes: **75 online devices running the
`d2722d3` agent payload**.

---

## 2. Agent payload equivalence across the maintenance line

| Ref | `edge-agent/` tree | Identical to baseline? |
|---|---|---|
| `d2722d3` (authoritative) | `fdea438fcf435bce6b4128697d3401a295c0b7da` | — |
| `e634e77` (maintenance head) | `fdea438fcf435bce6b4128697d3401a295c0b7da` | **Yes** |

**Verified.** All 16 commits between the two change only `cloud-api/`, `tools/`,
Alembic migrations, docs, and the UI artifact. No post-release commit altered
the agent payload.

**Consequence:** the maintenance line is agent-compatible with the deployed
fleet by construction. Any incompatibility introduced after `d2722d3` is
cloud-side or updater-side, not agent-side.

> Later commits on `release/edge-agent-0.1.9`, including `e634e77`, represent
> post-release development or maintenance candidates and do not redefine the
> original 0.1.9 release without separate approval and deployment evidence.

---

## 3. Cloud ↔ Edge compatibility — partially unverified

| Surface | State | Notes |
|---|---|---|
| Agent → FastAPI contract | `unverified` | `cloud-api/app/main.py` changed +143 lines after the baseline; contract impact not assessed |
| Cloud required edge release | `unverified` | `988641d` "Update cloud required edge release to 0.1.9" — gating behaviour not verified against the deployed agent |
| Gateway update schema | `unverified` | `2d64994`, `bad61ac` changed rollout queuing |
| Tunnel | `unverified` | `a2948b5`, `4267d17`, `e634e77` changed reconnect/replacement behaviour |
| Alembic head | `0023_edge_release_targets` | `0022`, `0023` added **after** the baseline |

**Migration compatibility is the highest-risk unknown.** Migrations `0022` and
`0023` do not exist at the authoritative agent baseline. If the live cloud
database has been migrated to `0023` while the fleet runs the `d2722d3` agent,
that combination has never been recorded as validated. `release-governance.md`
requires migrations to be additive and backward-compatible; whether `0022`/`0023`
satisfy that against the deployed agent is **unverified**.

Required next step: read the live `alembic_version` and compare against both
the baseline and the maintenance head. No database was contacted.

---

## 4. Agent ↔ Edge UI pairing — requires owner confirmation

The owner determination fixes the agent commit. The paired UI artifact changed
after the baseline.

| | At `d2722d3` | Current line (`e634e77`) |
|---|---|---|
| `edge_ui_tag` | `b4dc654` | `719d4a8` |
| `sha256` | `f7acfbaa…` | `a83fc2a6…` |
| UI trend gate present | **No** | **Yes** |

Re-paired by `84881fb`; `agent_source_commit` added by `d679871`.

**Likely deployed pairing (not verified):** agent `d2722d3` + UI `719d4a8`
(`a83fc2a6`), inferred from manifest lineage. **Requires owner confirmation.**

### Trend gate compatibility matrix

| Agent `local_edge_trends_enabled` | UI `EDGE_TRENDS_UI_ENABLED` | Result |
|---|---|---|
| `false` (0.1.9 default) | `0` (719d4a8 default) | **Approved 0.1.9 state** — trends off, UI shows "Coming in 0.2.0" |
| `false` | gate absent (`b4dc654`) | **Misleading** — trend UI reachable but never receives samples |
| `false` | `1` | **Misleading** — same as above |
| `true` | `0` | Samples collected, not viewable locally |
| `true` | `1` | Full trend operation — **not approved for 0.1.9** |

Both gates live in **different repositories**. There is no cross-repository
document requiring them to be set together. Enabling trends requires changing
both, and doing so departs from the approved 0.1.9 configuration.

---

## 5. Rollback compatibility

| Field | Value | State |
|---|---|---|
| Declared rollback target | `0.1.8` | Declared in `edge-0.1.9.json` and the release index |
| 0.1.8 artifact | none in workspace | **unverified** |
| 0.1.8 release note | none | **missing** |
| 0.1.8 manifest | none | **missing** |

**The declared rollback path is not currently reproducible from preserved
evidence.** Rollback for 0.1.9 therefore relies on gateway-local code-only
checkpoints (`/home/swadmin/gw-recovery/<release>/`) rather than on a
re-installable 0.1.8 artifact. Recovering the exact deployed 0.1.8 source and
artifact is an open work item. No 0.1.8 artifact may be reconstructed from an
unverified tag and presented as the deployed one.

---

## 6. Non-authoritative commits

| Commit | Classification | Compatibility relevance |
|---|---|---|
| `e634e77` | Later 0.1.9 development or maintenance-line head | Agent-identical; cloud-side impact unverified |
| `d899d80` | Contradicted or invalid 0.1.9 release candidate | Declares `0.1.8`; not deployed by the updater; must not be used as a compatibility reference |
| `71656b4` | Stale mutable release working copy | 7 commits behind; contaminated; not a compatibility reference |
