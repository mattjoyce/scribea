# ADR 0005 — Clip max duration = 10 minutes, enforced PWA-side

**Date:** 2026-05-22
**Status:** Accepted
**Decides:** specseed §14 Q5

## Context

The PWA records clips with `MediaRecorder` while the user holds a "recording" state. A forgotten stop (clinician walks away, screen lock, dead battery hides the UI) produces a runaway recording that fills IndexedDB and stresses the whisper service when it finally uploads.

## Decision

- **Hard cap: 10 minutes per clip.** The PWA enforces this by setting a `MediaRecorder` start time and calling `stop()` at the 10-minute mark, then immediately calling `start()` again to begin clip N+1.
- The PWA surfaces a visible countdown in the active-session screen when the clip is over 8 minutes — gives the clinician a chance to stop intentionally.
- Server-side cap (ingress side): clips reporting `duration_ms > 12 * 60 * 1000` (12 min, 20% slack) are rejected with `413 Payload Too Large` and `error: "clip_too_long"`. The PWA marks the clip `failed` in IndexedDB and shows the error.

## Rationale

- 10 minutes balances "long enough to capture a typical history segment without forcing artificial breaks" against "short enough that a forgotten stop doesn't ruin a session."
- PWA-side enforcement is the load-bearing line — if it works, the server cap never fires. The server cap exists for defense in depth (PWA bug, modified client).
- Auto-rolling to a new clip preserves continuity: the user doesn't even need to tap stop/start. They see the clip counter increment.

## Consequences

- The active-session screen runs a single `setInterval` watching elapsed clip time.
- Auto-rolled clips get a `meta.auto_rolled: true` flag in their baggage so the audit trail shows the boundary wasn't a user action.
- The assembled context's `[clip N, mm:ss]` markers continue to work — they're per-clip relative timestamps.

## When we revisit

If clinicians find the rollover disruptive or if 10 minutes turns out to chop a coherent narrative, we tune up. The cap is in one PWA module and one ingress check.

## Related

- specseed §9.2 (offline-first clip handling), §11.1 (transcribe-worker latency considerations).
