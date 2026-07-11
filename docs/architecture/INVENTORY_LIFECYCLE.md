# Inventory lifecycle foundation

Phase 1 adds a small, evidence-based lifecycle to saved BACnet inventory without changing the edge polling model.

## Record states

Saved devices and points record:

- `first_seen_at`: first cloud observation or approved import.
- `last_seen_at`: most recent successful discovery, object-list load, or approved import.
- `lifecycle_state`: currently `active` or `retired`.
- `retired_at`: timestamp recorded when an operator removes the device or point.

The existing `enabled` flag remains the UI/query visibility switch. Removing a device also retires its points. A subsequent successful discovery or point load reactivates a record and clears `retired_at`; this preserves stable record identity instead of duplicating inventory.

## Reconciliation path

1. The browser queues the existing `bacnet_discover` or `bacnet_load_points` edge job.
2. The edge agent executes its existing read-only BACnet CLI path and posts the result to the existing job-result endpoint.
3. The Cloud API validates the returned identifiers, updates known records, and adds newly discovered devices or loaded points.

No edge-agent update, new database, or external worker is required. Point loading is intentionally not treated as an authoritative full-device sweep because the existing job has an operator-selected limit; it therefore never automatically retires an absent point. Automatic “missing” or retirement policy should only be introduced with a complete-scan guarantee and an explicit approval workflow.
