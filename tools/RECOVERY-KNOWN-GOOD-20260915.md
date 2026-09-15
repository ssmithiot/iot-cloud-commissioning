# Known-Good Development Updater Recovery — 2026-09-15

This is the currently known-good recovery combination for the Windows **IOT
Edge Development Updater**. Use it when a later updater or Edge target set
needs to be abandoned.

## Trusted updater source

The trusted updater baseline is main commit:

~~~
53fc5dc0d0cbe7c7484534b09abfdc04fd30f911
~~~

It is the established 8/30 standalone-Python Development Updater baseline,
displayed as IOT Edge Development Updater 0.2.0-dev.2 and listening on
127.0.0.1:8791.

## Only known-functional Edge target combination (as of 2026-09-15)

Set these non-secret values in:

~~~
C:\ProgramData\IOT\EdgeDevUpdater\.env
~~~

~~~
IOT_EDGE_DEV_UI_COMMIT=2adae3adeb339806330db0e481cba3179fff2ff1
IOT_EDGE_DEV_AGENT_COMMIT=f77c42b88c5307009c35a2d94e5afbbdb3e4db98
IOT_EDGE_DEV_UPDATER_PORT=8791
~~~

IOT_EDGE_UPDATE_REF=32eaf06 may be retained for other tooling, but this
trusted updater baseline does not read it when resolving Development Updater
UI/Agent targets.

## Scope of this recovery

Restoring the Windows updater source and these target inputs does **not**
roll back an already-deployed gateway UI, Agent, router runtime, or gateway
configuration. Gateway rollback is a separate, explicit operation.

Do not place tokens, passwords, or .env contents in this document.
