# Isolated tunnel relay proof

This POC is a separate ASGI app, never imported by `app.main`; production
tunnel routes are unchanged. Run the relay with `uvicorn app.tunnel_relay_poc:app`.
The owner test process connects over the private owner endpoint and presents
`X-IOT-Relay-Owner-Auth`, populated from the server-only
`POC_INTERNAL_RELAY_SECRET`. No public headers are forwarded; the owner sees
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
