# ADR 0003 — Assembled context lives inline in baggage for v0

**Date:** 2026-05-22
**Status:** Accepted
**Decides:** specseed §14 Q3

## Context

The `assemble-worker` produces an "assembled context" — the ordered transcript with `[clip N, mm:ss]` markers and a session metadata header — and emits it inside `scribe.session.assembled.v1`. For a 60-minute consult this can grow large (tens of KB of text). Ductile messages above ~1 MB get awkward in some transports.

## Decision

For v0, **carry the assembled context inline in the event payload**.

The `assemble-worker` returns it under `event.payload.assembled_context` (UTF-8 text). The `structure-worker` consumes it the same way.

## Rationale

- A 60-minute clinical consult at ~150 wpm is roughly 9k words, ~50–80 KB of text. Far below message-size concerns.
- The simpler shape (one value flows through the pipeline) matches Hickey's "pass values, not objects" principle from specseed §3.1.
- Storing as a blob and passing a ref means we'd have to invent and maintain a "assembled-contexts" blob store path. Premature.

## Consequences

- Ductile baggage for the assemble→structure→format hop will be 50–100 KB for typical sessions. Acceptable.
- For multi-hour sessions (out of scope for v0) we'd need to switch to ref-passing.

## When we revisit

If a session pushes the assembled context above ~500 KB, or if we add a stage that wants to reference a specific historical assembled_context, we switch to:

1. `assemble-worker` writes the assembled context to a content-addressed blob (sha256) under `./blobs/contexts/`.
2. Emits `scribe.session.assembled.v1` with `payload.assembled_context_ref` instead of inline.
3. `structure-worker` resolves the ref.

The worker boundary is the only thing that changes — neither the event taxonomy nor downstream consumers' schemas change.

## Related

- specseed §3.1 (decomplecting transport from content), §6 (event types).
