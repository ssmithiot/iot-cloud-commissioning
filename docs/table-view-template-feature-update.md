# Table view template feature update

## Delivered

Commit `e2f2c42` is merged into `main` and pushed. Render may auto-deploy it from main.

The Gateway Workspace **Table View** toolbar now supports:

- **Export templates** — downloads one JSON template pack containing every saved local table view with its visible columns and selected BACnet object identities.
- **Import templates** — chooses that JSON file and a target controller, then recreates all matching table views for that controller.

## Mapping behavior

Saved table views previously stored browser-local point UUIDs. The export intentionally does **not** reuse those UUIDs. It records each selected point by normalized BACnet `object_type` and `object_instance`.

On import, each exported view is mapped to points on the selected target controller by that object identity. Imported names include the target controller name and avoid overwriting existing views. The UI reports matched and unavailable points.

## Scope and safety

- UI-only/browser-local feature; no database migration or new cloud API endpoint.
- Does not alter controller inventory, BACnet behavior, trend configuration, tunnels, or edge agents.
- Export supports multiple saved views in one template pack, which is intended for repeated-controller workflows.

## Verification

Focused UI tests passed:

```text
python -m pytest cloud-api/tests/test_api.py -k "exports_and_imports_saved_table_view_templates or table_view_defaults" -q
2 passed
```

## Follow-up ideas (not implemented)

- Add a preview before import showing matched/unmatched points per view.
- Allow applying one template pack to multiple target controllers in a single operation.
- Optionally persist/share template packs server-side instead of browser-local JSON downloads.
