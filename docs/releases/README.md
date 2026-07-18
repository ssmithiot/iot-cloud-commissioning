# Release Notes

This directory is the release record for Edge UI, agent, and cloud changes.
Every release note must state:

- release number and date;
- scope by component: Edge UI, edge agent, cloud, and configuration/data;
- immutable source artifact or commit;
- field verification and rollout order;
- rollback point and rollback method; and
- explicitly preserved gateway/site data.

Edge UI and edge-agent versions are independent. A UI-only rollout must not
change the reported agent version.
