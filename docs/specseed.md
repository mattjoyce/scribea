# Clinical Scribe MVP — Specseed

**Status:** Draft v0
**Date:** May 2026
**Purpose:** Learning prototype to understand the staging, pipeline, and observability of a clinical AI scribe end-to-end. Not for clinical use.

---

## 1. Goals

- Build a working PWA + backend that captures a multi-clip clinical consult, transcribes each clip, and produces a structured note from a hand-crafted template.
- Architecture must extend cleanly toward a real product without rewrite. NER, diarization, PHI redaction, hallucination verification, SNOMED normalization, eval grading, and richer observability are all later additions that slot in as new workers consuming existing events.
- Run on local infrastructure (home lab Docker on Unraid) with no third-party SaaS in the audio path. The LLM call may go to a hosted provider (Claude API) for v0; swappable to local Ollama later by changing the structure worker's config.
- Serve as a working demonstrator of the patterns to colleagues and AI vendors being evaluated — particularly the audit trail and provenance story.

## 2. Non-goals (v0)

- Real PHI handling, consent flows, audit certification.
- Diarization, NER, redaction, hallucination detection.
- Streaming / real-time partial transcription. Clip-batch is fine.
- In-PWA template editing or a template marketplace.
- Multi-user, auth, RBAC.
- HL7 / FHIR / SNOMED interop.
- Mobile-native packaging — PWA only.
- Production deployment, scaling, or HA.

## 3. Design principles

The architecture borrows from two intellectual traditions that share more than they differ. The synthesis is the point.

### 3.1 From Hickey: data over identity, simple over easy

- Every artifact (session, clip, transcript, structured note, baggage) is **a value** — a map of plain data with an explicit schema. Stages produce *new* values; they do not mutate existing ones in place.
- The domain event log (in `scribe.db` — see §3.3) is the source of truth for what happened to a session. Every other view (current session state, latest baggage, derived note) is *computed* from the log. This is information modeling, not object modeling.
- Identity is explicit and decoupled from data: `session_id`, `clip_id`, `event_id` are all UUIDs, all generated at creation time (often client-side), all immutable. The thing identified is not the same as the data about the thing.
- Schemas describe data; they are not classes. Validate at trust boundaries (HTTP edge, bus ingress, LLM response). Inside a worker, treat data as plain maps.
- Resist premature aggregation. The structure worker doesn't need a `Session` object with methods — it needs an ordered transcript and a template prompt. Pass values, not objects.

### 3.2 From Armstrong: let it crash, supervise, isolate

- Workers are isolated processes that communicate only via messages on the bus. No shared mutable state between workers. Persistent application state lives in `scribe.db`, single-writer-per-row; bus mechanics live separately in `ductile.db`, owned by Ductile (see §3.3).
- Workers do not defensively wrap operations they can't recover from. They fail loudly. The supervisor (Ductile) retries, routes around, or marks the work item failed and continues.
- Every message handler is **idempotent**. Replaying any message in the log must produce the same downstream effect, or be safely deduplicated. Combined with at-least-once bus delivery, this gives effectively-once semantics without distributed transactions.
- Partial failure is normal. A clip transcription dying mid-process is not an emergency — it's a `scribe.clip.failed.v1` event, and assembly proceeds with whatever clips made it through. The assembled context notes the gap; the structured note is honest about missing audio.
- The PWA treats the network as failable by default. Clips are local-first and uploaded with retry. The phone owns the clip until the server acknowledges it.

### 3.3 Decomplecting transport from content

A direct consequence of the two principles above, important enough to call out explicitly: the *bus* and the *app* keep separate databases.

`ductile.db` owns bus mechanics — message envelopes in flight, delivery attempts, retries, worker registry, supervisor state, dead-letter handling, cross-pipeline routing. It answers *what did the transport do?* and serves Ductile's own audit needs.

`scribe.db` owns domain content — sessions, clips, transcripts, structured notes, the domain event log, idempotency keys, `llm_cache`, prompt-version provenance. It answers *what happened in clinical terms?* and serves the scribe app's audit needs.

The two databases mirror each other in places. A `scribe.clip.transcribed.v1` event flows through Ductile (logged in `ductile.db`) and produces a row in `scribe.db.events` plus an update to `scribe.db.clips`. But the *facts* recorded are different. Ductile records "envelope `m_abc` was delivered to `transcribe-worker` on attempt 2 at 14:32:07 after a 240ms backoff." Scribe records "in session `sess_xyz`, clip 4 was transcribed using `whisper-medium.en` with these segment timings and this text." One is about the post office; the other is about the letter.

The bridge is the worker. A worker like `transcribe-worker` is *a Ductile worker* (consumes bus messages, emits bus messages) and *a scribe-app component* (reads and writes `scribe.db`). It translates between the two layers in each direction without either side needing to know about the other. Ductile never has to know what a clinical session is; `scribe.db` never has to know what a retry count is.

The operational property worth stating explicitly: `scribe.db` can be dropped and rebuilt from event-sourcing replay (because projections are derived) without touching Ductile, and Ductile's storage can be migrated or its bus implementation swapped without rewriting the scribe app. Either side surviving the rebuild of the other is the test of whether the separation is real.

The blob store (sha256-addressed audio files) is logically a third thing — neither bus mechanics nor relational domain data. A filesystem directory for v0; could become S3-compatible later without either database noticing.

### 3.4 Consequences of the synthesis

- The event log + idempotent handlers + immutable artifacts = full replay capability. Any session's state can be rebuilt from scratch by replaying its events. Debugging and testing become deterministic.
- Stage swaps are safe. You can replace `transcribe-worker-v1` with `transcribe-worker-v2` mid-session, because the events are the contract, not the worker.
- New stages (NER, redact, score) are **additive**, never destructive — they consume existing events and emit new ones. The existing workers don't know they exist.
- The "perceived complexity" of the system stays low because every component does one thing against well-defined inputs and outputs.

---

## 4. Domain model

### 4.1 Session

```json
{
  "session_id": "uuid",
  "template_id": "soap_consult",
  "state": "open | recording | closed | assembling | completed | abandoned | failed",
  "started_at": "iso8601",
  "closed_at": "iso8601 | null",
  "close_reason": "user | timeout | error | null",
  "clip_ids": ["uuid", "..."],
  "meta": {
    "user_agent": "string",
    "pwa_version": "string"
  }
}
```

### 4.2 Clip

```json
{
  "clip_id": "uuid",
  "session_id": "uuid",
  "seq": 0,
  "started_at": "iso8601",
  "duration_ms": 0,
  "audio_ref": "sha256:...",
  "state": "uploaded | transcribing | transcribed | failed",
  "transcript": null,
  "transcript_segments": [],
  "meta": {
    "stt_model": "whisper-medium.en",
    "stt_version": "...",
    "latency_ms": 0,
    "audio_format": "audio/webm; codecs=opus"
  }
}
```

`clip_id` is **client-generated** at the moment of `start` — this is the idempotency key that makes lossy networks survivable.

`audio_ref` is content-addressed (sha256 of the blob), which makes deduplication and replay trivial and means the blob store is a pure key-value store.

### 4.3 Template

A template is a hand-authored bundle that lives in the backend repo, version-controlled with the code:

```
templates/
  soap_consult/
    template.json          # metadata, prompt_id, prompt_version
    system_prompt.md
    schema.json            # JSON Schema for structured output
    render.tmpl            # Go template for markdown rendering
    few_shot/
      example_01.json
      example_02.json
```

`template.json` shape:

```json
{
  "template_id": "soap_consult",
  "version": "1.0.0",
  "name": "SOAP consult",
  "description": "Standard SOAP note for a general consultation",
  "prompt_id": "soap_consult_v1",
  "prompt_version": "1.0.0"
}
```

`GET /templates` returns metadata only (no prompts, no schema). The PWA caches metadata in IndexedDB on startup and shows them in the picker. Bumping a template version requires bumping `prompt_version` — the baggage trail must be honest about which prompt produced which note.

### 4.4 Baggage (the event envelope)

Every message on the bus, and every row in the event log:

```json
{
  "event_id": "uuid",
  "event_type": "scribe.clip.transcribed.v1",
  "event_time": "iso8601",
  "session_id": "uuid",
  "clip_id": "uuid | null",
  "data": { },
  "meta": {
    "worker": "transcribe-worker",
    "worker_version": "0.1.0",
    "prompt_id": null,
    "prompt_version": null,
    "model": "whisper-medium.en",
    "latency_ms": 0,
    "cost_usd": 0.0,
    "tokens_in": 0,
    "tokens_out": 0,
    "node": "unraid-01"
  }
}
```

The `meta` block is non-negotiable. Every stage stamps it. If the system can't tell you `worker_version`, `prompt_version`, model identity, and timing for every event, it is failing its primary learning purpose. This is also the table that powers later evals.

---

## 5. State machines

### 5.1 Session

```
  [POST /sessions]
        │
        ▼
      open ─────────────────► abandoned   (timeout, no first clip)
        │
   first clip arrives
        │
        ▼
    recording ───────────────► abandoned  (idle timeout)
        │
  POST /close OR safety timeout fires
        │
        ▼
     closed
        │
        ▼
   assembling ────────────────► failed    (assemble/structure error)
        │
        ▼
    completed
```

Each transition is an event on the bus. The `sessions.state` column is a projection — the latest known state, maintained by handlers. The authoritative state is reconstructable from `events`.

### 5.2 Clip

```
  [client uuid generated]
        │
        ▼
   queued (PWA-side IndexedDB outbox)
        │
   POST /sessions/{id}/clips with Idempotency-Key: clip_id
        │
        ▼
   uploaded ────────────► failed
        │
        ▼
   transcribing ────────► failed
        │
        ▼
   transcribed
```

`failed` clips do not block session assembly. The assembled context notes the gap (`[clip N: transcription failed — N seconds of audio missing]`) and assembly proceeds. This is the Armstrong move — a missing clip is not a session-killing event.

---

## 6. Bus message types

All messages carry the baggage envelope (§4.4). Event types are namespaced, versioned, and append-only.

| Event type                             | Emitted by         | Consumed by                  | `data` payload                                                |
|----------------------------------------|--------------------|------------------------------|---------------------------------------------------------------|
| `scribe.session.created.v1`            | HTTP ingress       | audit                        | `{ template_id }`                                             |
| `scribe.clip.received.v1`              | HTTP ingress       | transcribe-worker            | `{ audio_ref, seq, started_at, duration_ms }`                 |
| `scribe.clip.transcribed.v1`           | transcribe-worker  | live SSE, audit              | `{ transcript, segments }`                                    |
| `scribe.clip.failed.v1`                | transcribe-worker  | audit                        | `{ reason, stack }`                                           |
| `scribe.session.close_requested.v1`    | HTTP ingress       | assemble-worker              | `{ close_reason }`                                            |
| `scribe.session.assembled.v1`          | assemble-worker    | structure-worker             | `{ assembled_context, gaps }`                                 |
| `scribe.session.structured.v1`         | structure-worker   | format-worker                | `{ structured, raw_llm_response }`                            |
| `scribe.session.completed.v1`          | format-worker      | HTTP egress, audit           | `{ markdown, structured }`                                    |
| `scribe.session.failed.v1`             | any worker         | HTTP egress, audit           | `{ stage, reason }`                                           |

The `.v1` suffix is a hard rule. When a payload shape changes, emit `.v2` and run both consumers during the transition. There are no implicit migrations.

---

## 7. Storage

Per §3.3, storage is split into three concerns: bus mechanics (Ductile's responsibility), domain content (the scribe app's responsibility), and binary blobs (filesystem). Each is independently rebuildable.

### 7.1 ductile.db

Owned by Ductile. Schema not described here — refer to Ductile's own design docs for envelope storage, retry state, supervisor state, and worker registry. The scribe app does not read from or write to it directly; all interaction is through Ductile's bus API.

### 7.2 scribe.db (SQLite)

```sql
CREATE TABLE sessions (
  session_id    TEXT PRIMARY KEY,
  template_id   TEXT NOT NULL,
  state         TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  closed_at     TEXT,
  close_reason  TEXT,
  meta          JSON NOT NULL DEFAULT '{}'
);

CREATE TABLE clips (
  clip_id              TEXT PRIMARY KEY,
  session_id           TEXT NOT NULL REFERENCES sessions(session_id),
  seq                  INTEGER NOT NULL,
  started_at           TEXT NOT NULL,
  duration_ms          INTEGER NOT NULL,
  audio_ref            TEXT NOT NULL,
  state                TEXT NOT NULL,
  transcript           TEXT,
  transcript_segments  JSON,
  meta                 JSON NOT NULL DEFAULT '{}',
  UNIQUE (session_id, seq)
);

CREATE TABLE events (
  event_id     TEXT PRIMARY KEY,
  event_type   TEXT NOT NULL,
  event_time   TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  clip_id      TEXT,
  data         JSON NOT NULL,
  meta         JSON NOT NULL
);

CREATE INDEX idx_events_session ON events(session_id, event_time);
CREATE INDEX idx_events_type    ON events(event_type, event_time);
CREATE INDEX idx_clips_session  ON clips(session_id, seq);

CREATE TABLE idempotency (
  key          TEXT PRIMARY KEY,
  response     JSON NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE llm_cache (
  prompt_hash  TEXT PRIMARY KEY,
  model        TEXT NOT NULL,
  response     JSON NOT NULL,
  created_at   TEXT NOT NULL
);
```

`events` is append-only and authoritative *for domain history* — it is not the bus's transport log. `sessions` and `clips` are projections maintained by handlers; they exist for query convenience, not authority. If a projection ever disagrees with the event log, the event log wins and the projection is rebuilt by replaying events for the affected `session_id`.

`idempotency` and `llm_cache` are app-level caches with their own retention policies, unrelated to the event log.

### 7.3 Blob store

Audio files stored content-addressed by SHA256 under `./blobs/`. Each blob is referenced by `clips.audio_ref`. Filesystem for v0; can become S3-compatible later without either database changing. Blobs are not stored in SQLite (it ruins backup, replication, and WAL behaviour).

### 7.4 The bridge

A worker is the bridge between bus and storage. It receives a Ductile envelope, reads from `scribe.db` and the blob store as needed, performs its work, writes the domain event and any projection updates to `scribe.db`, and emits a new envelope onto the bus. Ductile sees only envelopes; `scribe.db` sees only domain content. Neither knows about the other's internals.

---

## 8. HTTP API

| Method | Path                            | Purpose                                                       |
|--------|---------------------------------|---------------------------------------------------------------|
| GET    | `/templates`                    | list available templates (metadata only)                      |
| POST   | `/sessions`                     | create session, returns `session_id`                          |
| GET    | `/sessions`                     | list sessions                                                 |
| GET    | `/sessions/{id}`                | session state + clip summaries                                |
| POST   | `/sessions/{id}/clips`          | upload a clip (multipart; `Idempotency-Key: <clip_id>`)       |
| GET    | `/sessions/{id}/clips`          | list clips with enrichment                                    |
| POST   | `/sessions/{id}/close`          | request close, fires `scribe.session.close_requested.v1`      |
| GET    | `/sessions/{id}/note`           | structured note (404 until state is `completed`)              |
| GET    | `/sessions/{id}/baggage`        | full event log for this session (debug view)                  |
| GET    | `/sessions/{id}/live`           | SSE stream of events for live view                            |

All POSTs accept `Idempotency-Key` and store `{key → response}` in the `idempotency` table for 24 hours. Resent requests return the cached response. This is the Armstrong move that makes flaky networks invisible without distributed-transaction machinery.

---

## 9. PWA architecture

### 9.1 Screens

- **Sessions list** — table of sessions (id, template, started, status, duration, clip count). Tap to open.
- **New session** — template picker (loaded from `/templates`, cached in IndexedDB), then "start session" → active screen.
- **Active session** — start/stop clip buttons, end-session button, live transcript pane subscribed to `/sessions/{id}/live`. Clip list with status indicators (`queued`, `confirmed`, `transcribed`).
- **Open session** — same layout as active but read-only, plus tabs for `baggage` (event log JSON), `structured` (LLM output JSON), `markdown` (final rendered note). Debug-by-default in v0.

### 9.2 Offline-first clip handling

The PWA owns the clip until the server acknowledges it. This is non-negotiable.

1. User taps **start**. PWA begins recording with `MediaRecorder`. PWA generates `clip_id` (uuid v4) and `started_at` immediately.
2. User taps **stop**. PWA stops recording, retrieves the blob, writes `{ clip_id, session_id, seq, started_at, duration_ms, blob, state: "queued" }` to IndexedDB in an `outbox` object store.
3. PWA sync logic attempts upload: `POST /sessions/{id}/clips` (multipart) with `Idempotency-Key: <clip_id>`.
4. On 2xx with matching `clip_id` in response: mark IndexedDB record `state: "confirmed"`. Keep the blob another 24 hours as safety, then evict.
5. On network failure or non-2xx: exponential backoff (1s, 2s, 5s, 15s, 60s, 5m, capped at 5m). Record stays in `queued`. UI shows a pending indicator on that clip.
6. On PWA reload: read `outbox`, resume sync attempts.
7. User taps **end session**: PWA waits up to N seconds (default 30) for all clips to reach `confirmed`. If any remain `queued`, prompt: "3 clips still uploading. Wait or close anyway?" Closing anyway leaves clips in `outbox`; the next session-open re-attempts upload.

**Edge case:** session has been closed server-side but the PWA still has an unconfirmed clip. For v0 the server rejects with `410 Gone`; the PWA marks the clip `orphaned` and exposes it in the debug view. Real handling (accept-as-late-arrival or reopen + reassemble) is post-MVP.

### 9.3 Service worker

Standard PWA service worker: cache the app shell + template metadata. Network-first for `/sessions/*` (always want fresh data); cache-first for static assets.

### 9.4 Storage

- **IndexedDB:** `outbox` (queued clips with audio blobs), `templates_cache`, `sessions_cache` (last viewed). Audio blobs stored directly as `Blob` (Chrome and Safari both support `Blob` values in IDB).
- **localStorage:** `last_session_id` for resume on PWA reload mid-session.

---

## 10. Templates

Templates are server-side, hand-authored, version-controlled with the backend repo. No in-PWA editing.

- `GET /templates` returns metadata only.
- The structure worker reads the prompt, schema, and render template directly from disk by `template_id`.
- The prompt-assembly logic is deterministic given template + assembled context:
  `prompt = system_prompt + few_shot_render + assembled_context + "Output JSON matching schema X."`
- Bumping `prompt_version` is mandatory when *any* file in the template directory changes. The baggage trail relies on it.

For v0 ship one template: `soap_consult`. SOAP fields are the four obvious ones (`subjective`, `objective`, `assessment`, `plan`). The schema can be trivially flat for v0; richer structure is a post-v0 evolution.

---

## 11. Workers (Ductile)

Each worker is a separate Go binary (or Ductile-managed handler). It subscribes to bus event types via Ductile, reads and writes `scribe.db` (and the blob store, where relevant), and emits new bus events. The worker is the bridge between `ductile.db` (transport) and `scribe.db` (domain), per §3.3 and §7.4. Every handler is idempotent against `event_id`.

### 11.1 `transcribe-worker`

- Subscribes: `scribe.clip.received.v1`
- For each event: load audio blob by `audio_ref`, invoke `faster-whisper` (model name in worker config), emit `scribe.clip.transcribed.v1` with text + segments.
- On failure: emit `scribe.clip.failed.v1` with reason. Do not retry inside the worker; Ductile's delivery semantics handle redelivery.
- Idempotency: if `clips.state` is already `transcribed` for this `clip_id`, no-op silently or emit a deduplicated event marker.

### 11.2 `assemble-worker`

- Subscribes: `scribe.session.close_requested.v1`
- Reads all clips for the session ordered by `seq`. For each: take the transcript, or mark the gap if `failed`. Composes the assembled context:
  - Session metadata header (template, total duration, clip count, gaps).
  - Ordered transcript with `[clip N, mm:ss]` markers so the LLM can cite.
  - (Entity index, speaker turns: not in v0.)
- Emits `scribe.session.assembled.v1`.
- Idempotency: pure function of clips + template_id. Re-running yields the same `assembled_context`.

### 11.3 `structure-worker`

- Subscribes: `scribe.session.assembled.v1`
- Loads the template by `template_id`. Composes the LLM prompt. Calls the configured LLM (Claude API for v0). Validates response against `schema.json`. On validation failure, retry once with an error nudge appended to the prompt; on second failure, emit `scribe.session.failed.v1`.
- Emits `scribe.session.structured.v1` with the parsed JSON + raw response.
- Idempotency: re-running gives a different LLM response in principle. To make replay deterministic, cache `(prompt_hash) → response` in `llm_cache`. Replays hit the cache; new requests hit the API. The cache key is the *content* of the assembled prompt, so any change to assembled context or template invalidates it correctly.

### 11.4 `format-worker`

- Subscribes: `scribe.session.structured.v1`
- Renders the structured JSON to markdown using the template's `render.tmpl` (a Go text/template for v0).
- Emits `scribe.session.completed.v1`.
- Idempotency: pure function of input.

---

## 12. Observability

- Every worker stamps `meta.worker`, `meta.worker_version`, `meta.latency_ms`, `meta.node` into baggage.
- Every LLM call additionally stamps `meta.model`, `meta.prompt_id`, `meta.prompt_version`, `meta.tokens_in`, `meta.tokens_out`, `meta.cost_usd`.
- The `events` table is the trace. Useful queries in v0:
  - p50 / p95 transcribe latency by audio duration
  - End-to-end time-to-note by template
  - LLM cost per session by template
  - Failure rates by stage
  - Drift after a `prompt_version` bump (diff structured outputs for same assembled_context)
- OpenTelemetry spans can be added later; the trace boundary already exists at each worker's event handler entry/exit.

---

## 13. Test and replay strategy

- The `events` table is replayable. Given a `session_id`, the projections (`sessions`, `clips`) can be rebuilt by reading events in order and applying each event's projection rule.
- A test harness writes a known event sequence and asserts the resulting projection matches expected. This catches projection bugs cheaply.
- End-to-end fixture: a recorded set of N clips (audio files) + an expected event sequence. Run through the workers with `llm_cache` pre-warmed; diff the final structured output against a golden file.
- The `llm_cache` table is what makes end-to-end tests deterministic without mocking. Hit the API once, capture the response, replay forever.

---

## 14. Open questions

1. **LLM choice for v0.** Claude (Anthropic API) for quality and reliable structured output, or local Qwen via Ollama for full local? Recommend Claude for v0 to remove one variable; swap to local once shape is stable. Worker config switch only.
2. **PWA framework or vanilla.** Vanilla HTML/JS keeps the build trivial, but the active-session screen has nontrivial state (IndexedDB sync, recorder lifecycle, SSE live view). Lit, Preact, or Svelte are all reasonable. Recommend vanilla + a few small modules for v0; reach for a framework only if the active screen becomes unwieldy.
3. **Where does the assembled context live?** Currently passed in baggage. Long transcripts (1hr+ consult) could push baggage above sensible message sizes (~1MB). For v0 keep it inline in baggage; for v1 store as a blob and pass the ref. Consider it ahead of time so the assemble-worker can be switched without consumer changes.
4. **Server-side session timeout.** Proposed 15 min. Configurable per template?
5. **Clip max duration.** Hard cap to prevent runaway recordings (e.g. forgotten stop). Suggest 10 min per clip; PWA forces a stop and starts a new clip if exceeded.

---

## 15. What slots in later (and where)

The architecture earns its keep if all of these are pure additions, not rewrites.

| Future capability                | Lands as                                              | Bus event(s)                                                     |
|----------------------------------|-------------------------------------------------------|------------------------------------------------------------------|
| Per-clip NER                     | new clip-pipeline worker after transcribe             | consumes `clip.transcribed`, emits `clip.enriched.v1`            |
| Per-clip diarization             | another clip-pipeline worker                          | consumes `clip.transcribed`, emits `clip.diarized.v1`            |
| PHI redaction                    | clip-pipeline worker                                  | consumes `clip.transcribed`, emits `clip.redacted.v1`            |
| SNOMED-CT-AU normalization       | session-pipeline stage between assemble and structure | consumes `session.assembled`, emits `session.normalized.v1`      |
| Hallucination scoring            | session-pipeline scorer after structure               | consumes `session.structured`, emits `session.scored.v1`         |
| Multi-scorer + veto fusion       | per the email-ingestion pattern in Ductile            | scorers emit, fusion worker decides                              |
| Streaming partial transcripts    | swap transcribe-worker to streaming Whisper           | extend `live` SSE payload; events unchanged                      |
| Clinical editing of the note     | new HTTP endpoints + new event types                  | `scribe.session.edited.v1`, with edit diffs persisted            |
| Multi-user / auth                | gateway in front of existing API                      | no event changes                                                 |
| FHIR / HL7 export                | terminal stage consuming `session.completed.v1`       | `scribe.session.exported.v1`                                     |

None of the above requires changes to the v0 workers, schema, or PWA contract. They are pure additions.

---

## 16. First implementation slice (the "hello clinic")

Smallest possible end-to-end:

1. One template: `soap_consult` with the flat-fields schema.
2. PWA with the four screens, IndexedDB outbox, no service worker yet.
3. `ingress` HTTP server with all endpoints in §8, persists events, fires bus messages.
4. `transcribe-worker` shelling out to faster-whisper CLI.
5. `assemble-worker` doing concatenation + markers.
6. `structure-worker` calling Claude API with template prompt, validating against schema.
7. `format-worker` running Go template.
8. SQLite with the schema in §7.
9. Manual end-to-end: record a 3-clip mock consult, watch it land in the open-session debug view.

When this works, the rest of the spec is mostly about not breaking what already works.

---

*End of specseed.*
