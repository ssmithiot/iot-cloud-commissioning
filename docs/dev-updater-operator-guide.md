# IOT Edge Development Updater — Operator Guide

**DEVELOPMENT UPDATER — MANUAL TEST GATEWAYS ONLY**

This program deploys Edge 0.2.0 release candidates to gateways you name, one at
a time. It is not the production updater. If you are updating a customer site on
Edge 0.1.9, close this and use the Legacy Edge Upgrade Webapp on port 8766.

## Before you start

You need the gateway's address, an SSH user and password, and a reason to be
touching that specific gateway. The application will not find gateways for you,
and there is no list to pick from by mistake.

Open **Start Menu → IOT Edge Development Updater**. It serves on
<http://127.0.0.1:8791/> and opens your browser. If port 8791 is busy the
application refuses to start and tells you so; run it again with `--port 8801`
or whichever port you prefer.

## The seven steps

### 1. Name the gateway

Type the address — for example `192.168.1.200` — the SSH user (`swadmin`) and
the password. The password is used for the connection and is never stored, never
written to the config, and never written to the log.

Press **Run read-only preflight**.

### 2. Read the preflight

The application connects and *looks*. It runs nothing that writes, restarts or
configures; every preflight command is checked for that before it is sent.

You get back: hostname, gateway ID, the current Edge UI commit and version, the
current Agent commit and version, `cloud_url`, both service states, the trend
sample count, free disk, and whether sudo will need a password.

**Read the `cloud_url` line.** It tells you which Cloud this gateway is talking
to. The application never changes it — but you should know which one you are
about to test against before you go further.

### 3. Pin the release

Choose the approved manifest (`edge-0.2.0.json`) and press **Resolve and verify
artifact**.

The application now pins everything: the exact Edge UI commit, the exact Agent
commit, the artifact filename and its SHA-256, which it verifies by hashing the
file. If the artifact has changed by a single byte since the release was
approved, you get a refusal with both hashes and nothing proceeds.

A manifest naming a branch, a tag, `origin/main` or a short SHA is refused. Only
full 40-character commits are accepted, because anything else can move under you
mid-deployment.

### 4. Choose the scope

There is no default. Pick one:

* **Edge UI only** — replaces Edge UI code. Does not touch the Agent, its
  configuration, or `iot-cx-agent.service`.
* **Edge Agent only** — moves the Agent to the approved commit and restarts it.
  Does not touch Edge UI files or data.
* **Edge UI + Edge Agent** — both.

### 5. Confirm

Tick the confirmation. If you chose anything involving the Agent, tick the second
one too: the Agent restarts and reconnects to its **existing** `cloud_url`, and
you are confirming you mean to do that on this gateway.

Press **Arm deployment**.

Changing the gateway, the manifest or the scope after this point silently clears
both confirmations. You will have to confirm again. That is deliberate.

### 6. Review the plan, then deploy

Every command that will run is listed, in order, with its stage. Read it. The
plan is refused before it ever reaches you if it contains a BACnet command, a
`cloud_url` assignment, a router service reference, a `git push`, or a BACnet
port assignment.

What happens when you press **Deploy**:

1. Preflight repeats.
2. A **code-only checkpoint** is created, checksummed with `sha256sum -c`, and
   proven readable with `tar -tzf`. If the checkpoint cannot be verified, nothing
   is applied.
3. The artifact's hash is verified *again*, on the gateway.
4. The payload is checked to contain no `data/`, no `.env`, no `start.sh`.
5. Your chosen components are applied.
6. Postflight reports the resulting commits, service states, `cloud_url` and
   trend sample count.

A failed step stops the run there. It does not continue, because a half-applied
update with a good checkpoint behind it is recoverable and a fully-applied
broken one is not.

### 7. If you need to go back

Press **Roll back**. The checkpoint is at:

```
/home/swadmin/gw-recovery/<rollback-release>/pre-update-code.tar.gz
```

Rollback is **code-only**. Trend data, saved devices, trend groups, samples,
`.env`, `start.sh`, credentials and gateway identity were never in the
checkpoint, so restoring cannot lose them.

Manual rollback, if the application is unavailable:

```bash
cd /home/swadmin/gw-recovery/0.1.9 && sha256sum -c pre-update-code.sha256
sudo systemctl stop edge-bacnet-ui.service
mkdir -p /tmp/edge-ui-code-restore && tar -xzf pre-update-code.tar.gz -C /tmp/edge-ui-code-restore
cd /tmp/edge-ui-code-restore && cp -a . /home/swadmin/edge-bacnet-ui-v2/
sudo chown -R swadmin:swadmin /home/swadmin/edge-bacnet-ui-v2
sudo systemctl start edge-bacnet-ui.service
```

## What this application will never do

It has no code path for any of these, and the tests prove it:

* Find gateways on its own, or update anything it found rather than what you typed.
* Update more than one gateway, run on a schedule, or deploy in the background.
* Change `cloud_url`, on any gateway, in any mode.
* Touch `bacrp`, `bacrpm`, `bacwp`, BACnet ports, BACnet locks, or the MSTP router service.
* Delete anything under `data/`, or overwrite `.env` or `start.sh`.
* Deploy Edge 0.1.9, or anything the Legacy Updater is responsible for.
* Modify, upgrade, reconfigure, or uninstall the Legacy Updater.
* `git push`, publish, or release.

## Logs

`%ProgramData%\IOT\EdgeDevUpdater\logs\IOTEdgeDevUpdater-YYYYMMDD.jsonl`

One JSON object per line: timestamp, operator, gateway, preflight result, current
and target versions, artifact hash verification, checkpoint path, what changed,
what was restarted, postflight, and where to roll back from.

Passwords, tokens and private keys are stripped on the way to disk — by field
name, and by pattern for anything that merely *looks* like a secret inside free
text. Logs survive uninstall; delete the folder by hand if you want them gone.

## When something goes wrong

**"cannot start: 127.0.0.1:8791 is already in use"** — another copy is running,
or something else took the port. Close the other copy, or use `--port`.

**"Refusing to use port 8766"** — that is the Legacy Updater's port. Pick another.

**"Release artifact SHA-256 does not match"** — the artifact is not the one the
release was approved with. Do not work around this. Re-install the MSI or fetch
the approved artifact again.

**"is a moving reference"** — the manifest names a branch or tag rather than a
commit. The manifest needs fixing; a development deployment must be reproducible.

**"Preflight failed"** — usually SSH. Check the address, the user, and that the
gateway is reachable. Nothing was changed.

**"Could not reach GitHub"** — the offline manifest and artifact installed with
the MSI are still usable. A refresh is a convenience, not a requirement.
