# Unified IOT Edge Development Updater

The Development Updater is the supported Windows operator workflow for an
existing Edge upgrade, a partial-install repair, or a fresh supported Ubuntu
UNO installation.  SSH/SFTP remains transport only; operators use the local
web UI.

## State matrix

| Detected state | Filesystem/service evidence | Recovery behavior |
| --- | --- | --- |
| `EXISTING_EDGE` | UI directory and Agent config/service exist | checkpoint, preserve runtime/site data, then upgrade |
| `PARTIAL_EDGE` | any UI, Agent, or checked-out repository component exists | checkpoint where possible, then idempotently repair missing components |
| `FRESH_LINUX` | none of the Edge UI, Agent config/service, or repository exists | bootstrap runtime; no rollback checkpoint is claimed |

Release compatibility is source-state based: the updater accepts fielded
0.2.0 and older releases through `EXISTING_EDGE`; version/commit collection is
diagnostic, while the selected immutable UI and Agent authorities are enforced
during deployment and verification.

The only intentionally unsupported case is bare hardware without a booted,
supported Ubuntu installation, network connectivity, SSH, and a sudo-capable
bootstrap account.
