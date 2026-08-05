# IOT Edge Development Updater — Operator Guide

**IOT Edge Development Updater — Manual Development Use**

This is the updater you already know. It is a copy of the working Legacy Edge
Upgrade Webapp with a different title, a different port, and its own
configuration directory. The form, the connection, the phases, the checkpoint,
the update steps and the rollback controls behave exactly as they do in the
updater you use today, because they are the same code.

If you are updating a customer site on Edge 0.1.9, close this and use the
existing updater on port 8766. This one is pinned to Edge 0.2.0.

## First run

1. Install `IOTEdgeDevUpdater-0.1.0-x64.msi`.
2. Copy your existing working `.env` to
   `C:\ProgramData\IOT\EdgeDevUpdater\.env`. The variable names are the same,
   so nothing in the file needs editing.
3. Open **Start Menu → IOT Edge Development Updater**.

It serves on <http://127.0.0.1:8791/>.

If the `.env` is missing, the page says so at the top and the terminal prints
the path it looked at plus the variable names it needs. If a required variable
is absent it names that variable. It never shows a value, and never writes one
to a log.

If port 8791 is busy the application refuses to start and says so; run it again
with `--port 8801`, or whichever port you prefer. It will not take port 8766 —
that belongs to the existing updater, and this program will not interfere with
it.

Both updaters can be open at the same time.

## Filling in the form

The same fields as the existing updater:

| Field | Example |
|---|---|
| Gateway ID | `GW006` |
| Site ID | auto-fills from the gateway number |
| Cradlepoint IP | `10.2.0.15` |
| Cradlepoint user | `BMS_admin` |
| Gateway host | `192.168.1.200` |
| Gateway user | `swadmin` |

Passwords come from your `.env` and are pre-filled as they are today.

The connection is unchanged: SSH to the Cradlepoint, then through to the
gateway behind it. If the Cradlepoint prompts to accept a host key, that is
handled for you, as before. Nothing connects directly to the gateway address —
it is always reached through the Cradlepoint you name.

## Choosing components

Use **Processes to run**, the same checkboxes as always. Three presets tick them
for you:

* **Edge UI only** — the UI phases only. The Agent is not updated, not
  restarted, and its configuration is left alone.
* **Edge Agent only** — the Agent phases only. No Edge UI file is replaced and
  UI runtime data is left alone.
* **Select all** — both, in the proven order.

You can still tick individual phases for a targeted rerun.

## What it will deploy

Shown at the top of the page:

* Edge UI `3246bff` on `release/edge-ui-0.2.0`
* Edge Agent `40133f2` on `release/edge-agent-0.2.0`

Those are the short forms of pinned commits. The full 40-character SHAs are what
gets checked out, validated, deployed and logged. There is no "latest" and no
branch tip that can move under you between one run and the next.

## Running an update

1. Leave **Dry run** ticked and press **Run Preflight**. Nothing on the gateway
   changes.
2. Read the phase results and the validation checklist.
3. Untick **Dry run**, tick the confirmation boxes, and press the button again.

A checkpoint is taken before anything is replaced, and the rollback controls at
the bottom — restore full backup, restore code-only checkpoint, list checkpoints
— work as they do in the existing updater.

## What is preserved

`cloud_url`, gateway and site identity, `data/**`, `edge-trends.db`, saved
devices, trend groups and samples, templates, programs, timed overrides, `.env`,
`start.sh`, BACnet ports, router configuration and gateway credentials. Service
configuration is left alone except where the component you selected requires a
restart.

## Differences worth knowing

**It will not claim cloud jobs.** The existing updater polls the cloud for
queued gateway updates and runs them unattended. This one does not, unless you
set `IOT_EDGE_DEV_UPDATER_CLAIM_CLOUD_JOBS=1`. It acts when you press the
button, and at no other time. Two updaters claiming from the same queue would
race for the same job.

**Its logs and configuration are its own.** Everything lives under
`C:\ProgramData\IOT\EdgeDevUpdater`. Uninstalling removes the program and its
shortcuts; it leaves that directory alone, so your `.env` and your logs survive
both upgrades and uninstall. Deleting them is your explicit choice.

**It cannot affect the existing updater.** Different port, different install
location, different configuration, different MSI UpgradeCode. It never reads or
writes the other program's files, and uninstalling one has no effect on the
other.
