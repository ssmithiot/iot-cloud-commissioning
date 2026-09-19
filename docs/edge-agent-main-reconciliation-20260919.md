# Edge Agent main reconciliation — 2026-09-19

The user requested that main include the currently working Edge Agent, paired
with the separately maintained `ssmithiot/edge-bacnet-commissioning-ui` main.

## Scope and provenance

- Previous main: `9cfadcb828130a4ba8b651abfb78b791a794babc`.
- Working Agent source: `d9232758b93fc9954a64235be918724df08238de`, the clean
  source revision installed on GW007.
- Integrate runtime changes under `edge-agent/` only. Keep main's existing
  cloud, development-updater, release selection, migrations and CI
  configuration. Supporting documentation and narrowly scoped test fixture
  corrections are recorded below.
- At reconciliation the Agent runtime/package version remained the working
  source's `0.2.3` (corrected from this record's initial `0.2.2` description).
- Do not merge the Agent branch's unrelated cloud/updater history wholesale.
- This main update does not deploy to any cloud server or gateway, rebuild an
  installer, replace a release artifact, or change existing updater pins.

The working Agent includes bounded local trend sampling, yielding to operator
BACnet work, saved routing metadata, durable local samples, suspended cloud
trend transport, traffic reporting and bounded long-poll command delivery.
Cloud trend uploads remain hard-disabled by `trend_cloud_upload_enabled()`.

## Required existing-gateway configuration

GW007 had local sampling enabled but no `edge_ui_data_dir`, causing the agent
to skip all local collection. With explicit approval on 2026-09-19, only the
following setting was added to its backed-up configuration, then only its
Agent was restarted:

```yaml
edge_ui_data_dir: /home/swadmin/edge-bacnet-ui-v2/data
```

`edge-agent/config.example.yaml` already contains this non-secret example
path. A package/source update **does not update an existing private config**.
For every target, confirm the actual Edge UI runtime directory and set the
agent to the same directory, with `local_edge_trends_enabled: true`.
Do not copy a gateway's real `agent.yaml`, tokens, `.env`, databases, captures
or startup scripts into source control.

GW007's effective configuration now finds its local database and saved route
for device 50, while cloud trend uploads stay disabled. Its USB/RS485 adapter
was absent and no local trend groups existed at the last check; real local
sample/viewer acceptance remains pending those prerequisites. Do not infer
hardware success from the configuration fix or passing isolated tests.

## Compatibility maintenance

The existing main release ledger references `docs/releases/edge/0.2.0.md`,
but that note was absent. Restore the historical note from the working branch
with an explicit historical-status banner. Do not change the ledger, release
selection or immutable artifacts.

Four alert tests on unchanged main still treated a two-hour-old heartbeat as
offline, although main now uses a six-hour threshold. Reproduced all four
failures against main's original Agent source. Update only those fixtures to
place their heartbeat one hour beyond the configured offline threshold;
retain every behavior assertion and leave production thresholds unchanged.

## Validation

- Complete Agent suite: **133 passed**, including local sample persistence
  and no-cloud-upload checks.
- Tools suite: **222 passed, 1 skipped** after restoring the historical note.
- Cloud compatibility suite: **345 passed, 6 failed, 1 deselected, 2 teardown
  errors**. The deselection is the existing CI exclusion for the gateway
  discovery-progress UI test; no new exclusions were added. Every remaining
  failure and both teardown errors reproduced with main's original Agent
  source and unchanged Cloud source/tests in the same environment:
  - expired in-memory tunnel lease close code (1008 versus expected 1013);
  - duplicate device response (200 versus expected 409);
  - legacy startup-DDL reconciliation (`mapping_templates` already exists);
  - heartbeat retention pruning;
  - two isolated relay proof-of-concept frame/restart tests, also producing
    their two subprocess teardown timeouts.
  These are not claimed fixed or treated as a green Cloud suite. Their
  runtime changes are outside this Agent promotion.
- Fresh scratch-database migration round-trip: upgrade to head, downgrade
  one revision, upgrade to head, all passed.
- Compile check passed for Cloud, Agent and tools.
- Runtime `edge-agent/iot_cx_agent/` matches working revision
  `d9232758b93fc9954a64235be918724df08238de` exactly.
- Cloud runtime, migrations, updater tools, deployment and CI configuration
  remain unchanged from main. No gateway or cloud deployment is performed.

## Rollout 0.2.5 version stamp

After reconciliation, the user designated the paired UI/Agent rollout as
`0.2.5`. Agent module and package metadata now declare `0.2.5`, alongside
the separately versioned Edge UI's `0.2.5` footer. The example configuration
also reports the paired UI as `0.2.5`; an existing private gateway config is
not overwritten and must describe the UI actually installed on that gateway.
The Agent's legacy missing-UI-version fallback is not evidence of an installed
UI upgrade and is left unchanged.

This update changes version identity, example configuration, tests and this
record only. Cloud trend transport remains disabled. Router/trend behavior,
Cloud runtime, updater defaults, historical release artifacts and gateway
services are not changed. Reconciliation validation above is historical;
this version stamp requires new immutable UI/Agent rollout commit pins.

Version-stamp validation: **134 Agent tests and 222 tools tests passed**, with
one existing tools skip. The new test verifies that loading the paired example
configuration reports both components as `0.2.5`. No Cloud tests were rerun for
this identity-only change; the pre-existing Cloud failures above remain open.

The Development Updater's existing release manifest still identifies `0.2.0`.
Its release-name field must match its selected manifest, independently of the
explicit source commit pins. A separately packaged `0.2.5` manifest is not
created or implied by this version stamp.
