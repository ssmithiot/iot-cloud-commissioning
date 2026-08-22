# GW006 trend overlap: read-only collection

Run these commands as the gateway service user. They only open SQLite in
read-only immutable mode and issue `SELECT` statements; do not substitute a
writable URI or run any command from a shell history that contains `UPDATE`,
`DELETE`, `VACUUM`, or `.dump`.

```bash
EDGE_DB='/home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db'
AGENT_DB='/var/lib/iot-cx-agent/edge.db'
sqlite3 "file:${EDGE_DB}?mode=ro&immutable=1" <<'SQL'
.headers on
.mode box
SELECT g.id AS group_id,g.name,g.enabled,g.interval_sec,p.id AS local_point_id,
       p.device_instance,p.object_type,p.object_instance,p.object_name
FROM trend_groups g JOIN trend_points p ON p.group_id=g.id
ORDER BY g.id,p.device_instance,p.object_type,p.object_instance;
SELECT COUNT(*) AS pending_local_outbox,MIN(o.created_at) AS oldest_pending,
       MAX(o.attempt_count) AS max_attempt_count
FROM trend_upload_outbox o WHERE o.state='pending';
SELECT o.id,o.event_id,o.attempt_count,o.next_attempt_at,s.sampled_at,
       p.device_instance,p.object_type,p.object_instance
FROM trend_upload_outbox o JOIN trend_samples s ON s.id=o.trend_sample_id
JOIN trend_points p ON p.id=s.trend_point_id
WHERE o.state='pending' ORDER BY o.id LIMIT 200;
SELECT * FROM trend_upload_quarantine ORDER BY quarantined_at DESC LIMIT 200;
SQL

sqlite3 "file:${AGENT_DB}?mode=ro&immutable=1" <<'SQL'
.headers on
.mode box
SELECT id,status,attempt_count,next_attempt_at,created_at,
       json_extract(payload_json,'$.point_id') AS point_id,
       json_extract(payload_json,'$.sampled_at') AS sampled_at
FROM sync_queue WHERE item_type='trend_sample' ORDER BY id LIMIT 200;
SELECT COUNT(*) AS pending_legacy,MIN(created_at) AS oldest_pending,
       MAX(attempt_count) AS max_attempt_count
FROM sync_queue WHERE item_type='trend_sample' AND status='pending';
SELECT transport,success,http_status,COUNT(*) AS requests,SUM(sample_count) AS samples,
       SUM(tx_bytes) AS tx_bytes
FROM trend_transport_events WHERE recorded_at >= datetime('now','-1 day')
GROUP BY transport,success,http_status ORDER BY transport,success,http_status;
SQL
```

To calculate exact physical-point overlap, save only the two result tables
(`device_instance`, `object_type`, `object_instance`) and compare those three
columns. A legacy queue payload contains a Cloud point UUID, so resolve it
against the Cloud's saved-point inventory before calling it a physical overlap.
