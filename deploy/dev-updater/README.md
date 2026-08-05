# IOT Edge Development Updater

A separately installed Windows application for deploying **Edge 0.2.0** to
gateways you name, one at a time.

**IOT Edge Development Updater — Manual Development Use**

It is a **copy of the working updater**, not a new program. The Cradlepoint
connection, the SSH host-key prompt handling, the nested SSH to the gateway
behind the Cradlepoint, gateway and site identity validation, the phase list,
checkpoint creation, the UI and Agent update steps, service restart and
verification, and the rollback reporting are the proven implementation,
unmodified. The interface is the same interface.

Only what separation and the 0.2.0 targets require was changed. Every change is
marked `DEV-UPDATER:` in `tools/dev_updater/updater_webapp.py`:

1. the import path, because the module sits one directory deeper
2. its own port, configuration directory, log directory and PID file
3. its own `.env` location, with a clear report when it is missing
4. its own copy of the release manifest
5. the product title
6. cloud job claiming off by default
7. two preset buttons that tick the existing component checkboxes

## The two programs side by side

| | Legacy Edge Upgrade Webapp | IOT Edge Development Updater |
|---|---|---|
| Runs | Jim's production work | Steve's development work |
| Port | **8766** | **8791** |
| How it is installed | `.cmd` launcher in a Git checkout | Windows MSI |
| Program files | the Git checkout | `C:\Program Files\IOT Edge Development Updater` |
| Configuration + logs | inside the checkout | `C:\ProgramData\IOT\EdgeDevUpdater` |
| `.env` | inside the checkout | `C:\ProgramData\IOT\EdgeDevUpdater\.env` |
| Python environment | `.gateway-update-venv` in the checkout | `C:\ProgramData\IOT\EdgeDevUpdater\venv` |
| Start Menu / Desktop entry | none | *IOT Edge Development Updater* |
| MSI UpgradeCode | none — not an installed product | `90FF1484-46DC-4848-890C-432F735E079D` |
| Cloud job claiming | on | **off** unless explicitly enabled |

Nothing in the right-hand column is shared with the left. They install, run and
uninstall independently, and are expected to run at the same time.

The Development Updater never imports, reads or writes the Legacy Updater's
module, launcher, manifest, configuration or logs.
`tools/tests/test_dev_updater.py` pins the Legacy Updater's file hashes and
fails if any of them changes.

## Install

1. Copy `IOTEdgeDevUpdater-0.1.0-x64.msi` to the Windows machine.
2. Double-click it, or `msiexec /i IOTEdgeDevUpdater-0.1.0-x64.msi`.
3. Copy your existing working updater `.env` to:

   ```
   C:\ProgramData\IOT\EdgeDevUpdater\.env
   ```

4. Start it from **Start Menu → IOT Edge Development Updater**, or the desktop
   shortcut.

Installing does not require the Legacy Updater to be stopped.

## Configuration

The `.env` uses **the same variable names as the existing updater**, so the
existing file can be copied across without rewriting it.

| Variable | Required | Purpose |
|---|---|---|
| `CRADLEPOINT_PASSWORD` | yes | Cradlepoint jump host |
| `GATEWAY_PASSWORD` | yes | gateway account behind it |
| `IOT_ADMIN_API_TOKEN` | no | only for cloud job claiming, which is off |
| `EDGE_UI_PASSWORD` | no | post-update UI authentication check |

`.env.example` beside the application lists these with descriptions and no
values. It is the only `.env`-shaped file in the MSI.

If the file is missing, the application says which path it looked at and which
variable names it needs. If a required name is absent, it names that variable.
**Values are never displayed and never written to a log.**

The `.env` is not in Git, not in the MSI, is preserved across MSI upgrades, and
is not removed on uninstall — the data directory carries no `RemoveFolder`, so
deleting it is a deliberate act.

## Ports

The Development Updater serves on **127.0.0.1:8791**.

* Configurable with `--port N` or `IOT_EDGE_DEV_UPDATER_PORT`.
* Availability is **detected, never assumed**: the port is bound for real at
  startup. On failure the application prints what is wrong and how to choose a
  different port, and exits with status 2 rather than serving.
* The Legacy Updater's port 8766 is reported read-only at startup so both can be
  seen at once. It is never bound, and nothing here can stop that process.

## What it deploys

Pinned commits, resolved from this product's own manifest copy at
`tools/dev_updater/releases/manifests/edge-0.2.0-dev.json`:

| Component | Branch | Commit |
|---|---|---|
| Edge UI | `release/edge-ui-0.2.0` | `3246bffd263f2e4a2bfaf033155052caf9a8bba7` |
| Edge Agent | `release/edge-agent-0.2.0` | `40133f2a81390db92a01b33a9c02c48a07363a7e` |

The interface shows the seven-character forms — `3246bff` and `40133f2`. The
full 40-character SHAs are what is used for checkout, validation, deployment and
logging. There is no `origin/main` target and no moving "latest": the Edge UI
branch has commits above this one, and the updater deploys the approved commit
rather than the branch tip.

The Edge UI artifact `gw006-edge-ui-0.2.0-dev-code.tar.gz` ships inside the MSI
so the updater works on a bench with no internet, and its SHA-256 is verified
before anything is sent to a gateway. It is **this product's own build of its
own pinned commit**, kept at `tools/dev_updater/releases/`. Jim's artifact at
`tools/releases/gw006-edge-ui-0.2.0-code.tar.gz` is a different commit, is named
by his manifest's checksum, and is never rebuilt or replaced here.

## Components

Use the **Processes to run** checkboxes, exactly as in the existing updater.
Presets tick the same boxes:

* **Edge UI only** — phases 0–5. No agent phase runs; the agent is not
  restarted and its configuration is untouched.
* **Edge Agent only** — phases 0, 7, 9, 10, 11. No UI file is replaced and UI
  runtime data is untouched.
* **Select all** — both, in the proven order.

## What is preserved on the gateway

Inherited from the copied implementation, not reimplemented: `cloud_url`,
gateway and site identity, `data/**`, `edge-trends.db`, saved devices, trend
groups and samples, templates, programs, timed overrides, `.env`, `start.sh`,
BACnet ports, router configuration, gateway credentials, and service
configuration except where the selected component requires a restart.

## Cloud job claiming

The updater this was copied from polls the cloud for queued gateway-update jobs
and runs them unattended. Jim's launcher suppresses that with `-NoClaim`, which
works by blanking the admin token in the process environment — and that cannot
work here, because a copied `.env` supplies the token from a file.

So claiming is **off** in the Development Updater. It acts only when you press
Update. Set `IOT_EDGE_DEV_UPDATER_CLAIM_CLOUD_JOBS=1` if you specifically want
it polling; two updaters claiming from the same queue would race for the same
job.

## Building the MSI

```
deploy/dev-updater/build-msi.sh [output-directory]
```

Needs `wixl` and `wixl-heat` from msitools; no Wine and no root. The build
refuses to produce an MSI if a `.env` or a populated credential variable is
found in the staged tree.
