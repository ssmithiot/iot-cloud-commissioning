# IOT Cloud Commissioning Architecture

IOT Cloud Commissioning starts with an outbound-only edge model.

- Edge gateways run `iot-cx-agent` on Ubuntu.
- BACnet activity remains local to each gateway.
- The edge agent stores local runtime state in SQLite.
- The cloud API stores enterprise records and current gateway status in PostgreSQL.
- Gateways report heartbeats to the cloud API over HTTP or HTTPS.

MVP-001 implements heartbeats only. Discovery, trends, jobs, remote writes, authentication, and UI workflows are later layers.

