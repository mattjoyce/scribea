# ADR 0004 — Session safety timeout = 15 minutes (configurable per template)

**Date:** 2026-05-22
**Status:** Accepted
**Decides:** specseed §14 Q4

## Context

Sessions that nobody closes need a safety timeout: a clinician forgets, the network drops, the PWA tab is closed. Without a timeout, sessions hang in `open` or `recording` forever and never reach assembly.

The spec proposes 15 minutes. Open question: per-template or global?

## Decision

- **Global default: 15 minutes of inactivity** (no new clip received) transitions an active session to `assembling` via a synthetic `scribe.session.close_requested.v1` with `close_reason: "timeout"`.
- **Per-template override**: a template can set `timeout_minutes` in its `template.json`. The ingress checks the session's template first, falls back to global.
- Sessions in `open` state (created but no first clip) get a shorter idle: **5 minutes**, after which they go to `abandoned` (not `assembling` — there's nothing to assemble).

## Rationale

- 15 minutes is long enough for a clinician to take a phone call mid-consult without losing the session, short enough to free up ingress state and notify the user before the day ends.
- Per-template override is cheap: one optional field in template.json, one fallback line in ingress. Avoids needing a config reload to support a long template (e.g. discharge planning).
- The shorter `open → abandoned` timeout reflects that an empty session is just session-id-pollution.

## Consequences

- Ingress runs a background sweeper goroutine on a 1-minute tick that scans sessions in `open` / `recording` state and emits the appropriate event.
- Sweeper writes the `scribe.session.timed_out.v1` event into `scribe.db.events` (audit trail), then submits `scribe.session.close_requested.v1` to Ductile.
- A clinician can resume a `recording` session that's about to time out by recording another clip — the inactivity clock resets.

## When we revisit

If clinicians complain about timeouts mid-flow or if abandoned sessions pile up, we tune the defaults. The config switch is one ingress restart.

## Related

- specseed §5.1 (session state machine), §11 (workers).
