# ADR 0002 — PWA uses vanilla JS + small modules (no framework)

**Date:** 2026-05-22
**Status:** Accepted
**Decides:** specseed §14 Q2

## Context

The PWA has four screens (Sessions list, New session, Active session, Open session). The active-session screen has nontrivial state: MediaRecorder lifecycle, IndexedDB outbox sync with exponential backoff, SSE live-transcript subscription, and three-way clip status (`queued / confirmed / transcribed`).

Framework choices considered: vanilla, Lit, Preact + signals, Svelte.

## Decision

Use **vanilla HTML + ES modules**, no build step. Each screen is a module under `pwa/`; shared state is in plain JS objects with a tiny pub/sub for cross-module updates.

## Rationale

- Zero build pipeline means the PWA edits faster than the backend: save, refresh, done. For a learning prototype that's the right tradeoff.
- The "nontrivial state" in the active screen is genuinely small — three IndexedDB stores and one SSE connection. Frameworks earn their keep when state crosses many components; here it doesn't.
- We pay no framework version-skew tax later. If a future screen genuinely needs reactive components (e.g. clinical editing per §15), we can introduce Lit at that point without rewriting what exists.

## Consequences

- No JSX, no TypeScript build. Plain `.js` files with `// @ts-check` doc-comments where useful.
- Outbox sync logic is hand-rolled. ~150 lines for the retry loop + IndexedDB wrapper.
- The PWA service worker is also hand-rolled (cache app shell, network-first for `/sessions/*`).

## When we revisit

If the active-session screen grows past ~600 lines of state management, or if we add a clinical-editing screen (§15) with form validation across many fields, we revisit and likely pick Lit.

## Related

- specseed §9 — full PWA architecture.
