# IOT Edge Development Updater

A separately installed Windows application for deploying **Edge 0.2.0 release
candidates** to hand-picked test gateways.

> **DEVELOPMENT UPDATER — MANUAL TEST GATEWAYS ONLY**
> Manual, one gateway at a time. No discovery, no batch, no schedule, no fleet push.

This is **not** the updater used for production sites. The Legacy Edge Upgrade
Webapp that Jim runs for Edge 0.1.9 is a different program, on a different port,
in a different directory, and this application cannot modify, upgrade,
reconfigure or remove it.

## The two programs side by side

| | Legacy Edge Upgrade Webapp | IOT Edge Development Updater |
|---|---|---|
| Purpose | Production sites, Edge 0.1.9 | Test gateways, Edge 0.2.0 RC |
| Port | **8766** | **8791** |
| How it is installed | `.cmd` launcher in a Git checkout | Windows MSI |
| Program files | the Git checkout | `C:\Program Files\IOT Edge Development Updater` |
| Data / logs | inside the checkout | `%ProgramData%\IOT\EdgeDevUpdater` |
| Python environment | `.gateway-update-venv` in the checkout | `%ProgramData%\IOT\EdgeDevUpdater\venv` |
| Start Menu entry | none | *IOT Edge Development Updater* |
| Releases it can deploy | whatever its manifest names | **0.2.0 only** |

Nothing in the right-hand column is shared with the left. They can run at the
same time, and are expected to.

## Ports and conflict detection

The Development Updater serves on **127.0.0.1:8791**.

* The port is configurable: `--port N`, or the `IOT_EDGE_DEV_UPDATER_PORT`
  environment variable.
* Availability is **detected, never assumed**. At startup the application binds
  the port for real — without `SO_REUSEADDR`, so a socket in `TIME_WAIT` cannot
  produce a false pass. If the bind fails it prints what is wrong, how to pick a
  different port, and exits with status 2 instead of serving.
* Passing `--port 8766` is refused outright: that port belongs to the Legacy
  Updater.
* The Legacy Updater's port is *reported* on the page, read-only, so both can be
  seen at once. It is never bound.

## Install

1. Copy `IOTEdgeDevUpdater-0.1.0-x64.msi` to the Windows machine.
2. Double-click it, or `msiexec /i IOTEdgeDevUpdater-0.1.0-x64.msi`.
3. Start it from **Start Menu → IOT Edge Development Updater**, or the desktop
   shortcut.

First run creates a private virtual environment under
`%ProgramData%\IOT\EdgeDevUpdater\venv` and installs `paramiko` into it. That
needs Python 3.10+ (`py -3`) and internet access **once**; afterwards it runs
offline. The Legacy Updater's own environment is untouched.

Silent install: `msiexec /i IOTEdgeDevUpdater-0.1.0-x64.msi /qn`

## Uninstall

Settings → Apps → *IOT Edge Development Updater* → Uninstall, or
`msiexec /x IOTEdgeDevUpdater-0.1.0-x64.msi /qn`.

Removes the program files and its shortcuts. **Logs and checkpoint records in
`%ProgramData%\IOT\EdgeDevUpdater` are NEVER removed on uninstall** — delete
that folder by hand if you want them gone. Nothing belonging to the Legacy
Updater is touched, because the MSI has no knowledge of it.

## Release source

Deployments resolve to exact, immutable bytes or they do not happen:

* The manifest is `tools/releases/manifests/edge-0.2.0.json`, shipped inside the
  MSI so the tool works on a bench with no internet.
* `edge_ui_tag` and `agent_source_commit` must each be a full 40-character
  commit. A branch, a tag, `origin/main`, `HEAD`, or an abbreviated SHA is
  refused with a message saying why.
* The artifact's SHA-256 is verified against the manifest before anything else
  happens, and verified **again on the gateway** after upload.
* Only releases in `APPROVED_DEV_RELEASES` (currently `0.2.0`) can be selected.
  0.1.9 is deliberately unreachable from this program.

The manifest list may be refreshed from GitHub, but a refresh cannot widen what
may be deployed — everything still goes through the same pinning and hash check.

## GitHub access

`ssmithiot/iot-cloud-commissioning` is **public**, so the manifest and the
release artifact are both retrievable **without credentials**. The application
sends no token by default and there is nowhere in the shipped product for one to
be embedded.

If the repository is ever made private, supply a token at runtime by either:

* setting `IOT_EDGE_DEV_UPDATER_GITHUB_TOKEN`, or
* creating `%ProgramData%\IOT\EdgeDevUpdater\github-token` containing the token.

Tokens are never written to source, the MSI, config files, installer properties,
or logs. Retrieval failures say exactly which of the two is configured and that
the offline copy can be used instead.

## Build from source

Requires `wixl` and `wixl-heat` from **msitools**. No Wine, no root.

```bash
deploy/dev-updater/build-msi.sh [output-directory]   # default: ./dist
```

Prints the MSI path, size, SHA-256 and the source commit it was built from.

## Safety model

* Gateways are typed in by hand. There is no discovery, and no path from
  discovery to deployment.
* Preflight is read-only, and every preflight step is checked for that before it
  is sent.
* Component scope has **no default** — Edge UI only, Edge Agent only, or both
  must be chosen deliberately.
* An Edge Agent update requires a **second, separate** confirmation.
* A verified checkpoint is taken, checksummed and proven readable **before** any
  change is applied.
* `cloud_url` is displayed before and after, and never written. A gateway on
  development, staging or production Cloud stays where it is.
* Changing the gateway, the target, or the component scope silently revokes any
  confirmation already given.
* Rollback is code-only. Trend data, saved devices, `.env`, `start.sh` and
  gateway identity are never in the checkpoint and cannot be lost by restoring
  it.
