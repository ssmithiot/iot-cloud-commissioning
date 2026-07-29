# PROJECT_STATE — Authoritative Current State

Machine-readable source: [`authority/project-state.yaml`](../../authority/project-state.yaml).
Release detail: [`RELEASES.md`](RELEASES.md) · [`authority/releases/0.1.9.yaml`](../../authority/releases/0.1.9.yaml).

Last updated: 2026-07-29. Authority repository: `iot-cloud-commissioning`.

---

## 1. Resolved: authoritative Edge Agent 0.1.9

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

**This question is closed.** No further work should be spent determining which
commit was the original Agent 0.1.9 release.

---

## 2. Trend status — approved temporary exception

Trends are **disabled by design in 0.1.9**, as an approved temporary exception.
This is **not** the permanent architecture.

| Gate | Location | Default | Introduced |
|---|---|---|---|
| `local_edge_trends_enabled` | `edge-agent/iot_cx_agent/config.py` | `False` | `d2722d3` (authoritative baseline) |
| `EDGE_TRENDS_UI_ENABLED` | `edge-bacnet-ui/app.py` | `"0"` | `719d4a8` |

Stated rationale, from `templates/edge_trends_disabled.html`: *"Local trend
collection is intentionally disabled in 0.1.9 while we complete a
non-interfering BACnet implementation."* BACnet performance is the stated
priority — MS/TP reads approximately 3 seconds, BACnet/IP reads under 1 second.

Restoring trends must not modify or degrade `bacrp` and `bacwp` behaviour. That
constraint governs the 0.2.0 trend work and is an open work item, not a defect
in 0.1.9.

Trend enablement requires **both** gates, set in **two different repositories**.
Enabling only the UI gate produces a trend UI that never receives samples. This
pairing is recorded in [`COMPATIBILITY.md`](COMPATIBILITY.md) and must be
documented wherever trend enablement is described.

---

## 3. Component state

| Component | Source | Release branch | Notes |
|---|---|---|---|
| edge-agent | `iot-cloud-commissioning/edge-agent/` | `release/edge-agent-0.1.9` | `main` still declares `0.1.6` — **contradicted** |
| edge-ui | `edge-bacnet-ui` | `release/edge-ui-0.1.9` | Dual orphaned histories, both preserved |
| cloud-api | `iot-cloud-commissioning/cloud-api/` | — | Alembic head `0023_edge_release_targets` |

`edge-agent/` at the workspace root is a planning placeholder containing only a
README; it holds no Agent source and explicitly defers to the monorepo.

---

## 4. Repository roles

| Repository | Role | Authority |
|---|---|---|
| `iot-cloud-commissioning` | Cloud API, Edge Agent, release tooling, ledger | **Authority repository** |
| `edge-bacnet-ui` | Edge UI source | Active source |
| `engineering-headquarters` | Workstation and platform standards | **Not** application authority; no remote |
| `docs/`, `gateway-tools/` | Empty scaffolds, 0 commits | None |
| `releases/` | Mutable working copies | Evidence only, contaminated |

`engineering-headquarters` must not become the assumed source of truth. Its own
README scopes it to the workstation and disclaims gateway and cloud source.

---

## 5. Readiness

**State: not ready** for Engineering Authority handoff.

| ID | Open item | Severity |
|---|---|---|
| OPEN-01 | Exact deployed 0.1.8 source and artifact unrecovered | Critical |
| OPEN-02 | Paired Edge UI artifact for the 75-device rollout unconfirmed | High |
| OPEN-03 | Maintenance-line approval and version identity for `e634e77` undecided | High |
| OPEN-04 | Rule #1 enforced in code but wording unstated anywhere | High |
| OPEN-05 | Trend ownership rule unratified and absent from all repositories | High |
| OPEN-06 | Supabase error investigation not started | High |
| OPEN-07 | Two parallel migration systems | Medium |
| OPEN-08 | `engineering-headquarters` has no approved independent remote | Medium |

Resolved since the Phase 1 audit: the authoritative Agent 0.1.9 identity
(OPEN, now closed by owner determination), and the loss risks addressed by the
Phase 2A preservation record at
`forensic-preservation/2026-07-29/PRESERVATION_REPORT.md`.

---

## 6. Preserved evidence

| Item | Location |
|---|---|
| Phase 1 audit findings | Session record |
| Phase 2A preservation report | `forensic-preservation/2026-07-29/PRESERVATION_REPORT.md` |
| Uncommitted MS/TP + BBMD work | branch `preserve/2026-07-29-gw006-mstp-bbmd-wip`, commit `71dd49f`, pushed |
| Orphaned Edge UI histories | branches `archive/edge-ui-pre-recovery-0.1.8` / `-0.1.9`, pushed |
| `engineering-headquarters` | Git bundle in the preservation store (same-disk risk remains) |
