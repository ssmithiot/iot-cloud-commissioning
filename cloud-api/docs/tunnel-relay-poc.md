# Isolated tunnel relay proof

This POC is a separate ASGI app, never imported by `app.main`; production
tunnel routes are unchanged. Run the relay with `uvicorn app.tunnel_relay_poc:app`.
The owner test process connects over the private owner endpoint and presents
`X-IOT-Relay-Owner-Auth`, populated from the server-only
`IOT_TUNNEL_RELAY_INTERNAL_SECRET`. No public headers are forwarded; the owner sees
only a tunnel identifier and opaque text/binary frames.

Each direction has one copy task. It awaits the peer `send_text`/`send_bytes`
before receiving another frame, with no `asyncio.Queue`; TCP/ASGI flow control
therefore bounds relay buffering. A close/failure cancels both tasks, closes
the opposite socket, and removes the pair. The relay never reconnects.

Agent `d9232758` uses JSON request/response messages over the existing public
WebSocket protocol, with base64 payload fields. The relay preserves those text
frames unchanged and also copies binary bytes unchanged. Gateway handshake
accounting remains at Agent-to-public-Cloud connect; gateway payload accounting
remains at the owner/gateway boundary. Render-private Cloud-to-owner bytes are
not Cradlepoint/gateway traffic and must not be counted there.

For staging: deploy a public Cloud Web Service and a same-region private
`tunnel-owner` service, exactly one owner instance. The public service uses
`ws://tunnel-owner:$PORT` through Render private DNS. Give both the internal
secret; owner needs `PORT`, environment marker, secret, a dedicated Uvicorn
startup command, and private `/health`. Per active tunnel the public service
uses two WebSockets, two relay tasks, and roughly two socket FDs plus small
task buffers; the owner uses one private WebSocket plus its live gateway state.
That is reasonable for a small operator-requested tunnel count; throughput is
limited by the slower peer rather than by an unbounded relay queue.

## Production canary gate

The production route remains on the legacy manager unless both
`IOT_TUNNEL_RELAY_ENABLED=true` and the exact authenticated gateway ID appears
as a comma-separated item in `IOT_TUNNEL_RELAY_CANARY_GATEWAYS`. There are no
wildcards, prefixes, site, or version rules. The initial value may be `GW017`.
Only selected IDs connect outbound to `IOT_TUNNEL_RELAY_OWNER_URL/{gateway_id}`
using `IOT_TUNNEL_RELAY_OWNER_SECRET`. If no owner is present the selected
connection closes with 1013 and does not fall back. Heartbeats, jobs, trends,
status, and all idle Agent behavior are untouched because the gate executes
only after an Agent has already initiated the existing tunnel WebSocket.

The standalone private owner entrypoint is
`app.tunnel_relay_owner_service:app`. It exposes `/health` and the private
WebSocket `/internal/tunnel-relay/owner/{gateway_id}`. Set the public Cloud
variable `IOT_TUNNEL_RELAY_OWNER_URL` to the Render private address including
that path but excluding `/{gateway_id}`, for example
`ws://<private-host>:10000/internal/tunnel-relay/owner`.

The owner is not an echo endpoint: it registers the relayed Agent WebSocket in
the existing `TunnelManager`, resolves the existing JSON response protocol,
owns `TunnelSessionManager` sessions, and serves authenticated private
status/session/request/close APIs. Public GW017 status and proxy routes call
those owner APIs; non-canary gateways retain public Cloud's legacy managers.

## Durable GW017 canary control plane

When the exact-ID relay gate selects GW017, `tunnel/open` upserts the existing
`GatewayTunnelRequest` primary-key row with state `requested`, operator,
requested duration, and expiry. `tunnel/close` marks that row `closed` and
expires it immediately. An active authorization is `requested` with an expiry
later than server time; expired rows are inactive without cleanup work.

The selected gateway's one held 600-second `/jobs/next` request releases its
database session before each wait and rechecks this durable row at most every
10 seconds. This is Cloud-side polling inside the held request, not new Agent
HTTP traffic. Non-canary gateways retain the original single Condition wait
and perform zero added durable tunnel rechecks. Production schema verification:
`cd cloud-api && alembic current` must show revision `0025_gateway_tunnel_requests`
or later, and the `gateway_tunnel_requests` table must be present.
