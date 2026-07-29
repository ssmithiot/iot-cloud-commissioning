# GW006 Edge BACnet Router authority model

## Purpose

The Edge BACnet Router is an enable/disable authority layer over the existing
standard BAS routing configuration. It is not a second destructive profile and
must never overwrite or delete the BASRT-B/BAC-RTR configuration.

## Canonical persisted state

Persist these independently:

```yaml
saved_standard_bacnet_route: basrtb
edge_bacnet_router_enabled: false
edge_bacnet_router_configuration:
  endpoint: 192.168.1.200:47809
  agent_fdr_port: 47814
  mstp_network: 202
  serial_device: /dev/ttyUSB0
  baud: 38400
  mstp_mac: 2
  max_master: 127
  max_info_frames: 128
```

`saved_standard_bacnet_route` is the operator's most recently selected
standard BAS route. It remains unchanged while the Edge router is enabled.

## Routing authority states

| State | Standard BAS routes | Edge router service | Agent/UI BACnet path |
| --- | --- | --- | --- |
| Disabled | Active and selectable | Stopped and disabled; serial released | Saved standard BAS route |
| Enabled | Preserved but inactive/unselectable | Active; owns UDP 47809, USB, token state, and MS/TP network 202 | Local FDR: source 47814 to `192.168.1.200:47809` |

### Enable transaction

1. Validate the serial device, router binary, UDP 47809 availability, and
   compatible agent/UI configuration.
2. Back up the service, agent configuration, saved standard route, and UI
   runtime state.
3. Preserve the selected standard route in `saved_standard_bacnet_route` and
   deactivate only its runtime use.
4. Start and enable `iot-cx-mstp-router.service` with MS/TP network 202.
5. Apply agent FDR configuration: source UDP 47814 and BBMD
   `192.168.1.200:47809`.
6. Make Edge UI BACnet operations use the local-router path.
7. Validate FDT, discovery, read, safe write/relinquish, serial ownership, and
   no cross-delivered traffic.
8. If any validation fails, restore the entire backed-up standard runtime
   state automatically.

### Disable transaction

1. Back up the current edge-router and standard-route state.
2. Stop and disable `iot-cx-mstp-router.service`; confirm `/dev/ttyUSB0` is
   released.
3. Remove/deactivate the agent's local FDR runtime configuration.
4. Restore the saved standard route without asking the operator to re-enter
   it.
5. Make the Edge UI use that restored standard route.
6. Validate the standard BAS route. If restoration fails, roll back to the
   pre-disable edge-router state.

## UI contract

Under **Edge**, add **Edge BACnet Router** before the visually separated
**Restart Edge Agent** action.

The page has one master control:

`Edge BACnet Router: [Disabled / Enabled]`

When disabled, show the active standard BAS route, allow the global route
selector to operate, and show the local router as stopped/inactive.

When enabled, show that standard routes are temporarily inactive; lock the
normal route selector; and show the local endpoint, FDR source port, MS/TP
network, serial device/settings, and live service status. Never delete the
saved standard route.

The right-aligned global route control is:

- Disabled: `BACnet route: [saved standard BAS route ▾] [Apply]`
- Enabled: `BACnet route: [Edge BACnet Router / 47809 / Agent 47814]`
  with the standard selector disabled.

## Acceptance criteria

- Enable and disable are transactional and automatically roll back on failed
  validation.
- The standard BAS route is restored exactly after disable.
- The router service and USB serial ownership exist only while enabled.
- FDR exists only while enabled.
- UI route controls accurately reflect routing authority.
- BASRT-B and BAC-RTR settings remain persistent and are never destructively
  replaced by USB-485 settings.
