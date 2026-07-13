# Parallel Development Protocol — Codex + Claude

Context: the platform is live to a small internal group only; a bad deploy is recoverable by rolling back to a stable release and is not customer-facing. This protocol is sized for that reality — minimal ceremony, but hard rules where mistakes are expensive regardless of audience (schema, uncommitted work, security surfaces).

## Ownership zones

| Zone | Owner | Branch |
|---|---|---|
| `cloud-api/app/ui.py`, UI-only features, browser-local behavior | **Codex** | `main` |
| API routes (`main.py`), models, **all Alembic migrations**, auth, access, config, edge agent, tunnel, BACnet, tools, release/ops docs | **Claude** | feature branch (currently `codex/trend-hardening`) |

Zero-overlap zones = merges stay trivial. The current branch has no real changes in `ui.py`, so Codex has a clean sandbox there.

## Hard rules (the ones that prevent real headaches)

1. **Codex never creates Alembic migrations or touches `models.py`.** Two branches minting migration numbers produces a two-headed schema — the one merge problem that's genuinely painful. Anything needing schema goes to Claude (or Claude specs it first).
2. **Codex never touches** tunnel code, BACnet write behavior, auth/access, edge agent, or `config.py`.
3. **No uncommitted long-lived work.** Claude's phase work gets reviewed, committed, and pushed promptly. Uncommitted trees are the biggest integration risk in this repo's history — bigger than anything Codex does.
4. **Line endings:** before committing branch work, whitespace-only file churn gets reverted so diffs contain only real changes.

## The flow for "I need a small UI feature"

1. Owner gives Codex the task **with the guardrail preamble below**. Claude's work is not interrupted.
2. Codex implements on `main`, additive only, with focused tests and a `docs/<feature>.md` note (the table-view feature is the reference example — that shape worked).
3. Owner tells Claude "Codex merged X." Claude fetches, reviews the commit, and logs it. Expected response: "got it, tracked." That is the normal case.
4. Ask Claude **before** (not after) only when the feature needs: a new API endpoint, a schema/migration change, background processing, or anything in Claude's zone.

## Codex guardrail preamble (paste into every Codex task)

```
Scope rules for this task:
- Work from latest main. Additive changes only.
- UI-only: you may modify cloud-api/app/ui.py and add focused tests in
  cloud-api/tests/test_api.py.
- Do NOT create or modify Alembic migrations, models.py, config.py, auth.py,
  access.py, tunnel code, BACnet write behavior, or anything under edge-agent/.
- Do NOT add new API endpoints; use existing endpoints only. If the feature
  needs a new endpoint or schema change, stop and report instead.
- Run the focused tests you add/touch and include the command + result.
- Write a short docs/<feature-name>.md describing what changed, mapping
  behavior, scope/safety, and verification (see
  docs/table-view-template-feature-update.md for the expected shape).
```

## Merging main into the feature branch

Periodically (and always before a staging deploy), the branch integrates main. Because of the zones, expected conflicts are near zero; if `ui.py` conflicts due to residual whitespace churn, **take main's `ui.py`** — the branch has no real changes there. Claude reviews every integration but does not perform merges (owner or Claude Code does, per the working agreement).

## Deploys (current, internal-only posture)

- Worst case today is a briefly broken internal UI, recovered by redeploying the previous release — acceptable.
- Once staging exists (setup in progress): deploys to production follow `docs/release-process.md` with the read-only smoke check; auto-deploy posture gets reconfigured at that time.
- Regardless of audience, never deploy a ref containing an un-applied migration mismatch — the app refuses to start on schema drift by design, which turns that mistake into downtime rather than corruption.

## When this protocol gets stricter

Revisit (tighten ceremony, require pre-merge review of Codex commits) when any of: first external customer user, the 160-site rollout makes the UI operationally critical to field work, or Codex tasks start needing endpoints/schema regularly.
