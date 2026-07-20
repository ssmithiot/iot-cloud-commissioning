# GW006 controlled Edge rollback test — operator runbook (2026-07-20)

**Purpose:** prove the GW006 rollback archive is restorable to the same
checkpoint before any production promotion (handoff Remaining Work Item 1).
This is a *restore-to-same-checkpoint* test: the archive captured the current
rollout state at 2026-07-20T11:44:48Z, so after restore the gateway must be in
the same state it is in now. The test validates the archive, not a downgrade.

**Scope guards:** GW006 only. Never touch GW015. Nothing here modifies
`/home/swadmin/edge-bacnet-ui-v2/data` or `edge-trends.db`. Do not paste the
contents of `agent.yaml` or `edge-agent.env` anywhere — they hold credentials;
only the whitelisted `grep` lines below are safe to record.

**Facts (verified from repository):**
- Agent checkout: `/home/swadmin/iot-cloud-commissioning` (service ExecStart uses `edge-agent/.venv`)
- Config: `/etc/iot-cx-agent/agent.yaml` + `/etc/iot-cx-agent/edge-agent.env` (SECRETS)
- Unit: `/etc/systemd/system/iot-cx-agent.service` (User=swadmin)
- Edge UI: `edge-bacnet-ui.service`, `/home/swadmin/edge-bacnet-ui-v2`, data in `data/`, trends DB `data/edge-trends.db`
- Staging API: `https://iot-cloud-api-staging.onrender.com`
- Pre-rollout git baseline (for reference only, not this test): `dbb74c3` on `main`

Record every command output in the results template at the bottom. Any step
marked **GATE** that fails means STOP — do not proceed to the next phase.

---

## Phase 0 — archive existence, ownership, checksum

```bash
A=/var/lib/iot-cx-agent/gw006-edge-rollback-20260720T114448Z.tar.gz
sudo test -f "$A" && echo "archive present" || echo "FAIL: archive missing"
sudo stat -c 'owner=%U:%G mode=%a size=%s mtime=%y' "$A"
```

**GATE:** archive present. If owner/mode are not `root:root 600` (handoff notes
it was created 644 and a correction was issued but never confirmed):

```bash
sudo chown root:root "$A" && sudo chmod 600 "$A"
sudo stat -c 'owner=%U:%G mode=%a' "$A"    # must now print root:root 600
```

Checksum (record the value; also stored beside the archive):

```bash
sudo sha256sum "$A" | sudo tee "$A.sha256"
```

## Phase 1 — inspect contents WITHOUT extracting  **GATE**

```bash
sudo tar -tzf "$A" > /tmp/rollback-manifest.txt
wc -l /tmp/rollback-manifest.txt
head -40 /tmp/rollback-manifest.txt
```

All four checks must print `OK`:

```bash
# 1. Absolutely no Edge UI data or trend DB in the archive:
grep -E 'edge-bacnet-ui-v2/data|edge-trends\.db' /tmp/rollback-manifest.txt \
  && echo "FAIL: archive would overwrite Edge UI data - STOP" || echo "OK: no Edge UI data"
# 2. No absolute paths (must be relative so extraction lands under / predictably):
grep -E '^/' /tmp/rollback-manifest.txt && echo "FAIL: absolute paths" || echo "OK: relative paths"
# 3. No path traversal:
grep -F '..' /tmp/rollback-manifest.txt && echo "FAIL: traversal" || echo "OK: no traversal"
# 4. Only expected trees (agent repo, agent config, unit file, agent var dir):
grep -Ev '^(\./)?(home/swadmin/iot-cloud-commissioning/|etc/iot-cx-agent/|etc/systemd/system/iot-cx-agent\.service|var/lib/iot-cx-agent/)' \
  /tmp/rollback-manifest.txt | head && echo "CHECK ANY LINES ABOVE" || true
```

For check 4: any lines printed are unexpected members — evaluate each before
proceeding; anything under `edge-bacnet-ui-v2` or unrelated system paths is a
STOP.

## Phase 2 — capture pre-rollback state (redacted)

```bash
systemctl is-active iot-cx-agent.service edge-bacnet-ui.service
git -C /home/swadmin/iot-cloud-commissioning rev-parse HEAD           # PRE_HEAD - record
git -C /home/swadmin/iot-cloud-commissioning branch --show-current    # record
# SAFE config keys only - never cat the whole file:
sudo grep -E '^(cloud_url|local_edge_trends_enabled|cloud_trend_sync_enabled|local_edge_trend_cloud_sync_enabled|local_edge_trend_upload_interval_sec|local_edge_trend_upload_batch_size|edge_ui_data_dir):' \
  /etc/iot-cx-agent/agent.yaml
# Edge UI data reference points (pre):
ls /home/swadmin/edge-bacnet-ui-v2/data | wc -l
find /home/swadmin/edge-bacnet-ui-v2/data -name '*.json' | wc -l
stat -c 'size=%s mtime=%y' /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db
sqlite3 /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db 'PRAGMA quick_check;'
```

Record: PRE_HEAD, config keys, file counts, DB size/mtime, `quick_check` = ok.

## Phase 3 — the rollback

```bash
sudo systemctl stop iot-cx-agent.service
systemctl is-active iot-cx-agent.service      # expect "inactive"
sudo tar -xzpf "$A" -C /
sudo systemctl daemon-reload
sudo systemctl restart iot-cx-agent.service
sleep 10
systemctl is-active iot-cx-agent.service      # GATE: must print "active"
journalctl -u iot-cx-agent.service -n 30 --no-pager   # review; REDACT any token-looking strings before sharing
```

If the agent is not active: `journalctl -u iot-cx-agent.service -n 100` for the
error, then restore forward is trivial because this was a same-checkpoint
restore — investigate before touching anything else.

## Phase 4 — verification  **GATE (all must pass)**

```bash
# 1. Restored checkpoint matches the archive's captured state:
git -C /home/swadmin/iot-cloud-commissioning rev-parse HEAD           # POST_HEAD == PRE_HEAD expected
# 2. Edge UI untouched and alive:
systemctl is-active edge-bacnet-ui.service                            # active
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/       # 200 (or the UI's known port)
ls /home/swadmin/edge-bacnet-ui-v2/data | wc -l                       # == Phase 2 count
find /home/swadmin/edge-bacnet-ui-v2/data -name '*.json' | wc -l      # == Phase 2 count
sqlite3 /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db 'PRAGMA quick_check;'   # ok
stat -c 'size=%s mtime=%y' /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db
#    (mtime should keep ADVANCING over the next minutes - sampler still writing)
# 3. Heartbeat resumed (staging):
journalctl -u iot-cx-agent.service --since '-3 min' --no-pager | grep -iE 'heartbeat|200' | tail -5
#    and confirm GW006 shows online in the staging workspace UI.
# 4. Targeting after restore (handoff item 12 - determine, do NOT change):
sudo grep -E '^cloud_url:' /etc/iot-cx-agent/agent.yaml
#    Expected: https://iot-cloud-api-staging.onrender.com (archive captured the
#    staging-pointing rollout state). Whatever it prints, record it and leave it.
# 5. Outbox/trend sync still healthy after restart:
journalctl -u iot-cx-agent.service --since '-3 min' --no-pager | grep -iE 'trend|outbox|snapshot' | tail -10
```

## Phase 5 — verdict

**PASS requires every one of:** archive root:root 600 with recorded sha256;
manifest clean of Edge UI data/absolute paths/traversal; agent active after
restore; POST_HEAD == PRE_HEAD; Edge UI service active and serving; data file
counts unchanged; `edge-trends.db` quick_check ok and mtime advancing;
heartbeat 200s resumed against the URL recorded in step 4; no errors in agent
journal. Anything else is **FAIL** — record which gate failed and stop.

---

## Results template (fill in, redact secrets)

| Item | Value |
|---|---|
| Operator / date | |
| Archive owner:mode (final) | |
| sha256 | |
| Manifest line count / gate results | |
| PRE_HEAD / branch | |
| POST_HEAD | |
| Edge UI service pre/post | |
| data file count pre/post | |
| edge-trends.db quick_check pre/post | |
| edge-trends.db mtime advancing post-restore | |
| Heartbeat evidence (journal line time + staging UI online) | |
| cloud_url after restore (item 12) | |
| VERDICT | PASS / FAIL |
