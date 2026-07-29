# GW006 Edge UI navigation staging patch

Date: 2026-07-23

The deployed UI is a non-Git, locally customized tree at
`/home/swadmin/edge-bacnet-ui-v2`. Its navigation is controlled solely by
`templates/base.html`; `edge-bacnet-ui.service` starts `start.sh`, which runs
`/usr/bin/python3 app.py`.

## Immutable gateway record

The exact before/after copies, unified diff, checksums, service context,
deployment script, validation response, and rollback source are retained on
GW006 at:

`/home/swadmin/gw006-usb485-staging/20260723T012719Z/ui-navigation-20260723T022835Z`

Relevant files:

- `original/base.html` — pre-change file, SHA-256
  `906f48d4b4d238dc3c1488c26ba3d03c3cb7231a47e39d04395d72872d0a35b0`
- `candidate-base.html` — deployed file, SHA-256
  `895031f010f96dce04350b63401bb1debd3de2624d7a0475493e9b41a5444037`
- `base.html.patch` — complete unified diff
- `context/start.sh` and `context/edge-bacnet-ui.service` — deployment context
- `deploy-ui-navigation.sh` — guarded deployment; on validation failure it
  restores `original/base.html` and restarts the UI.

## Changes

Only `templates/base.html` changed. Existing Flask endpoints, authorization,
route selector submission, and restart handler were retained. The navigation
now groups existing links into Devices, Configure, Diagnostics, Edge, and
Account menus; Home remains direct; Write PV is labelled **Single Point Read /
Write**; and the pre-existing restart confirmation remains inside the visually
separated Edge restart action.

This deployment predates the reversible Edge BACnet Router authority model.
Adding the **Edge BACnet Router** menu item, page, master enable/disable
control, and disabled-standard-route UI is a separate pending change specified
in `docs/gw006-edge-router-authority-model.md`; it must not be inferred as
already deployed from this navigation-only record.

## Deployment and validation

The file was copied from `candidate-base.html`, then
`edge-bacnet-ui.service` was restarted. Jinja parsing and route-reference
checks passed before deployment. The post-deployment UI service was active and
served the login page after its expected authentication redirect.

Rollback command:

```bash
cp /home/swadmin/gw006-usb485-staging/20260723T012719Z/ui-navigation-20260723T022835Z/original/base.html \
  /home/swadmin/edge-bacnet-ui-v2/templates/base.html
sudo systemctl restart edge-bacnet-ui.service
```
