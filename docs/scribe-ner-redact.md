# scribe-clinical-ner and scribe-redact — plugin specs

**Status:** Draft v0
**Parent:** [`specseed.md`](./specseed.md), [`scribe-audio-preprocess.md`](./scribe-audio-preprocess.md)
**Scope:** Two new per-clip plugins in v0 NOP form, with attention to how their new values attach to existing baggage and projections.

---

## 0. Preamble — value attachment

Both plugins are *additive*: they consume an existing event, produce a new event, and add new facts about an existing artifact (the clip). Nothing about existing events, projections, or contracts changes.

Three rules govern how new values attach. They are the same rules `scribe-audio-preprocess` follows; spelling them out here keeps every per-clip plugin honest.

**1. New events, never modified events.** Neither plugin re-emits or supersedes `scribe.clip.transcribed.v1`. Each emits its own event with its own version suffix. Consumers who don't care about NER or redaction continue to read `scribe.clip.transcribed.v1` and ignore the rest.

**2. New columns on the projection, never repurposed columns.** The `clips` table gains explicit new columns (`entities`, `redacted_transcript_ref`, `redactions`) rather than overloading existing ones. The `transcript` column always holds the original. The redacted transcript is reached via `redacted_transcript_ref` into the blob store. This means a SQL reader can always answer "what did the STT actually produce?" without consulting downstream plugins.

**3. New blobs, never edited blobs.** When `scribe-redact` produces a redacted transcript, it writes a new content-addressed blob and emits a new ref. The original is never overwritten. In NOP mode, the new ref *equals* the original ref (same content → same sha256), and that's the correct behaviour — the audit trail shows redaction ran and produced an identical output, which is a different fact from "redaction didn't run."

These three rules make every fact retraceable and every plugin replayable without coordination.

---

## Plugin 1 — `scribe-clinical-ner`

### 1.1 Purpose

Find named entities in a clip's transcript and emit them as structured spans. In v0, runs in NOP mode and emits an empty entity list — but with the full envelope, latency stamp, and projection update. The slice exists so substance can drop in later without touching contracts.

The plugin handles both **clinical entities** (medications, conditions, procedures, anatomy, dosages) and **PHI entities** (persons, dates, MRNs, addresses) under a single controlled vocabulary. Whether one extractor produces both, or two extractors produce one each and the plugin merges, is an internal detail.

### 1.2 Position

```
preprocess → transcribe → ner → redact → assemble → ...
```

Per-clip stage, parallel across clips within a session.

### 1.3 Subscribes / emits

| Event                          | Direction | Payload                                                                  |
|--------------------------------|-----------|--------------------------------------------------------------------------|
| `scribe.clip.transcribed.v1`   | in        | `{ transcript, segments }` (from transcribe-worker)                      |
| `scribe.clip.entities.v1`      | out       | `{ entities[], extractor, stats }`                                       |
| `scribe.clip.ner_failed.v1`    | out       | `{ reason, stage }` on failure                                           |

### 1.4 Event payload

`scribe.clip.entities.v1` data block:

```json
{
  "entities": [],
  "extractor": {
    "name": "scribe-clinical-ner",
    "model": "nop",
    "version": "0.1.0",
    "ontology": null
  },
  "stats": {
    "transcript_chars": 1842,
    "entities_found": 0,
    "by_type": {}
  }
}
```

When a real extractor replaces NOP, `extractor.model` changes ("scispacy-en_core_sci_md", "gliner-v1", etc.), `extractor.ontology` populates ("UMLS", "SNOMED-CT-AU", "ad-hoc"), `entities` fills in, and `stats.by_type` reports counts per type. No other field shape changes.

### 1.5 Entity shape

Each entity in the array:

```json
{
  "type": "MEDICATION",
  "text": "paracetamol",
  "start": 142,
  "end": 153,
  "confidence": 0.94,
  "code": null,
  "code_system": null
}
```

- `type` — controlled vocabulary (§1.6)
- `text` — surface form as it appears in the transcript
- `start`, `end` — character offsets into `transcript` (not token offsets)
- `confidence` — extractor confidence in [0, 1], or `null` if not provided
- `code`, `code_system` — populated by a later normalisation step (e.g., SNOMED-CT-AU mapping); always `null` here

### 1.6 Controlled vocabulary

| Type           | Examples                                                  | PHI? |
|----------------|-----------------------------------------------------------|------|
| `MEDICATION`   | paracetamol, Panadol, atorvastatin, salbutamol            | no   |
| `DOSAGE`       | 1 g, 500 mg, two tablets, 10 mL                           | no   |
| `CONDITION`    | hypertension, asthma, T2DM, chest pain                    | no   |
| `PROCEDURE`    | ECG, spirometry, biopsy, X-ray                            | no   |
| `BODY_PART`    | left knee, lower back, abdomen                            | no   |
| `PERSON`       | Mr Smith, Dr Patel, the patient's wife                    | yes  |
| `DATE`         | 23 May 2026, last Tuesday, two weeks ago                  | yes  |
| `MRN`          | medical record numbers, patient IDs                       | yes  |
| `ADDRESS`      | 12 Smith St Croydon Park                                  | yes  |
| `PHONE`        | phone numbers                                             | yes  |

The PHI flag is a property of the type, not the entity. `scribe-redact` filters by PHI=yes to decide what to mask. Types are extensible; adding a new type requires bumping the event version to `.v2`.

### 1.7 Projection: `clips.entities`

New JSON column on `clips`:

```sql
ALTER TABLE clips ADD COLUMN entities JSON DEFAULT NULL;
```

Populated by the projection handler on `scribe.clip.entities.v1`. `NULL` means NER hasn't run yet (or failed). `[]` means NER ran and found nothing. Distinguishing those two states matters for debugging.

### 1.8 NOP behaviour

NOP mode is the v0 default:

- Receive `scribe.clip.transcribed.v1`
- Read transcript length from event data (no DB read needed)
- Sleep zero milliseconds
- Emit `scribe.clip.entities.v1` with empty entities, `extractor.model: "nop"`, populated `stats.transcript_chars`
- Update `clips.entities = '[]'` projection
- Stamp `meta.latency_ms` (will be ~5ms, dominated by DB write)

A real extractor swap changes only the inner extraction logic. The event, projection, and contract are unchanged.

### 1.9 Failure modes

| Failure                          | Response                                                                                                |
|----------------------------------|---------------------------------------------------------------------------------------------------------|
| Extractor model load fails       | Emit `scribe.clip.ner_failed.v1` with `reason: "model_load"`. Clip continues — redact handles empty.    |
| Extractor crashes on a clip      | Emit `scribe.clip.ner_failed.v1` with `reason: "extraction_error"`, stack trace in meta.                |
| Transcript missing or empty      | Emit success with empty entities; `stats.transcript_chars: 0`. Not a failure.                           |

Per Armstrong: a NER failure does not block redact. Redact handles missing entities gracefully (see §2.7).

### 1.10 Idempotency

Keyed on `event_id`. If `clips.entities` is already non-NULL for this `clip_id`, no-op and emit a deduplicated marker (or skip emission entirely; see specseed §3.2).

### 1.11 Environment

| Env var                       | Default     | Notes                                                       |
|-------------------------------|-------------|-------------------------------------------------------------|
| `NER_MODE`                    | `nop`       | `nop`, `scispacy`, `gliner`, `regex`                        |
| `NER_MODEL_PATH`              | -           | required if not `nop`                                       |
| `NER_LABEL_SET`               | (see §1.6)  | comma-separated, for GLiNER                                 |
| `NER_DEVICE`                  | `cpu`       | `cpu` or `cuda`                                             |

---

## Plugin 2 — `scribe-redact`

### 2.1 Purpose

Produce a redacted transcript by masking PHI entity spans. In v0, runs in passthrough mode — the redacted blob has the same content as the original. Downstream stages consume the redacted ref by default. When real redaction is needed, only this plugin's logic changes.

### 2.2 Position

```
preprocess → transcribe → ner → redact → assemble → ...
```

Per-clip stage, runs after NER, parallel across clips.

### 2.3 Subscribes / emits

| Event                            | Direction | Payload                                                                                                |
|----------------------------------|-----------|--------------------------------------------------------------------------------------------------------|
| `scribe.clip.entities.v1`        | in        | `{ entities[], extractor, stats }` (from NER)                                                          |
| `scribe.clip.redacted.v1`        | out       | `{ redacted_transcript_ref, original_transcript_ref, redactions[], passthrough }`                      |
| `scribe.clip.redact_failed.v1`   | out       | `{ reason }` on failure                                                                                |

### 2.4 Event payload

`scribe.clip.redacted.v1` data block:

```json
{
  "redacted_transcript_ref": "sha256:a1e...",
  "original_transcript_ref": "sha256:a1e...",
  "redactions": [],
  "passthrough": true,
  "redactor": {
    "name": "scribe-redact",
    "mode": "passthrough",
    "version": "0.1.0",
    "mask_strategy": "none"
  }
}
```

In passthrough mode (v0 default), `redacted_transcript_ref == original_transcript_ref` because the content is identical and the blob store dedupes by sha256. This is correct and honest: redaction ran, produced no changes, the audit trail records that fact distinctly from "redaction didn't run."

When real redaction runs, the refs differ, `passthrough` becomes `false`, `mode` becomes "active", `mask_strategy` describes the replacement strategy ("placeholder", "type-token", "asterisks"), and `redactions[]` enumerates what was changed.

### 2.5 Redaction shape

Each redaction in the array (empty in passthrough):

```json
{
  "entity_type": "PERSON",
  "original_text": "Mr Smith",
  "replacement_text": "[PERSON]",
  "start": 234,
  "end": 242,
  "source_entity_index": 7
}
```

`source_entity_index` references the position in the NER event's `entities` array, so the audit trail can trace a redaction back to the entity that triggered it.

### 2.6 Transcript blob storage

The original transcript was already content-addressed and stored as a blob by `transcribe-worker` (or, equivalently, persisted in `clips.transcript` with an implicit sha256). For the redaction event to carry refs to *both* original and redacted, the original transcript needs to live in the blob store under a stable sha256 key.

Two options for v0:

- **Option A** (simpler): keep `clips.transcript` as the original text in the DB. `original_transcript_ref` is computed as `sha256(clips.transcript)` at the moment redact runs, and the same hash is the key for any redacted version. Blob store writes happen only when redaction actually changes content.
- **Option B** (cleaner): write the original transcript to the blob store at transcribe time, store the ref in `clips.transcript_ref`, and treat `clips.transcript` as a derived view. Redact writes redacted blobs to the same store.

**Chosen for v0: Option A with materialisation** — `scribe-redact` always writes the original transcript to the blob store at `sha256(transcript)` (idempotent via CAS — same content dedupes to the same path). In passthrough mode, `redacted_transcript_ref == original_transcript_ref` and exactly one blob exists per distinct transcript. `assemble-worker` always reads via the ref. `clips.transcript` remains independently queryable from SQL per §0 rule 2.

### 2.7 NOP / passthrough behaviour

- Receive `scribe.clip.entities.v1`
- Read the original transcript from `clips.transcript`
- Compute `original_ref = sha256(transcript)`
- Write the transcript to the blob store at `original_ref` (idempotent — skip if blob exists)
- In passthrough mode: emit with `redacted_transcript_ref = original_ref`, `redactions: []`, `passthrough: true`
- Update `clips.redacted_transcript_ref` projection (= original_ref in passthrough)
- Stamp `meta.latency_ms`

In active mode (post-v0): filter entities by `PHI=yes`, apply mask strategy in reverse offset order (to keep earlier offsets valid), write the new text to the blob store, emit with the new ref.

### 2.8 Projection: `clips.redacted_transcript_ref` and `clips.redactions`

```sql
ALTER TABLE clips ADD COLUMN redacted_transcript_ref TEXT DEFAULT NULL;
ALTER TABLE clips ADD COLUMN redactions JSON DEFAULT NULL;
```

`redacted_transcript_ref` is `NULL` until redact runs; equals the original's sha256 in passthrough; differs in active mode. `redactions` is `NULL` until run, `[]` in passthrough, populated in active mode.

### 2.9 Failure modes

| Failure                           | Response                                                                                                |
|-----------------------------------|---------------------------------------------------------------------------------------------------------|
| Missing entities event (NER failed) | Run in degraded passthrough — emit success with empty redactions, but stamp `redactor.warnings: ["ner_unavailable"]`. Better to flow through with an unredacted transcript and a warning than to stall the pipeline. |
| Blob write fails                  | Emit `scribe.clip.redact_failed.v1` with `reason: "blob_write_failed"`.                                  |
| Transcript missing from `clips`   | Emit failed with `reason: "no_transcript"`. Should not happen post-transcribe; if it does, projection is broken. |

The "degraded passthrough on missing NER" rule is deliberate. It means the pipeline always has a transcript to assemble, even if NER and redact both failed. The honesty cost is paid in baggage warnings, not in pipeline stalls.

### 2.10 Idempotency

Keyed on `event_id`. If `clips.redacted_transcript_ref` is already non-NULL, no-op. The blob store dedupes by content-address, so re-running redact on the same input produces the same output ref naturally.

### 2.11 Environment

| Env var                         | Default        | Notes                                                                          |
|---------------------------------|----------------|--------------------------------------------------------------------------------|
| `REDACT_MODE`                   | `passthrough`  | `passthrough` or `active`                                                      |
| `REDACT_MASK_STRATEGY`          | `placeholder`  | `placeholder` (`[PERSON]`), `type-token` (`[PERSON_1]`), `asterisks` (`****`)  |
| `REDACT_PHI_TYPES`              | `PERSON,DATE,MRN,ADDRESS,PHONE` | which types to redact when in active mode                                      |

---

## 3. Changes to existing plugins

These two new plugins require one change to one existing plugin. Stated explicitly so it doesn't get lost.

### 3.1 `assemble-worker`

Currently reads `clips.transcript` directly. Now reads from `clips.redacted_transcript_ref` by default — fetching the (possibly redacted, possibly passthrough) transcript from the blob store.

An env var on assemble controls the source:

| Env var                 | Default      | Notes                                                                                  |
|-------------------------|--------------|----------------------------------------------------------------------------------------|
| `ASSEMBLE_TRANSCRIPT_SOURCE`  | `redacted`   | `redacted` (read via `redacted_transcript_ref`) or `original` (read `clips.transcript`) |

The value used for each session is stamped into the assemble event's baggage:

```json
{
  "assembled_context": "...",
  "gaps": [...],
  "transcript_source": "redacted"
}
```

This is the single most important new field in the assemble event. It says, in the audit trail, exactly which version of each transcript the LLM saw. Without it, every later question ("did the LLM see PHI?", "was this run pre- or post-redaction?") becomes archaeology.

### 3.2 No other plugins change

`transcribe-worker`, `structure-worker`, `format-worker` are unchanged. The contracts they read and emit are stable. This is the test of whether the new slices are truly additive.

---

## 4. Changes to specseed.md

These edits should land in the same PR that adds the two plugin specs.

- **§6 Bus message types** — add four rows: `scribe.clip.entities.v1`, `scribe.clip.ner_failed.v1`, `scribe.clip.redacted.v1`, `scribe.clip.redact_failed.v1`.
- **§7.2 scribe.db schema** — add `clips.entities`, `clips.redacted_transcript_ref`, `clips.redactions` columns.
- **§11 Workers** — add `11.5 scribe-clinical-ner` and `11.6 scribe-redact` subsections (brief — refer to this spec for detail). Update §11.2 `assemble-worker` to note the transcript-source flag.
- **§15 What slots in later** — remove the "Per-clip NER" and "PHI redaction" rows; they are no longer "later," they are NOP'd in v0. Optionally add a row noting the swap-from-NOP path.
- **§16 First implementation slice** — extend to seven plugins: ingress + transcribe + assemble + structure + format + ner (NOP) + redact (passthrough). Note that NER and redact don't gate the "hello clinic" demo working — they just emit and stamp.

---

## 5. Tests

### 5.1 NER NOP

- Given a `scribe.clip.transcribed.v1` with a 100-word transcript, the plugin emits `scribe.clip.entities.v1` within 50ms with `entities: []` and `stats.transcript_chars` correct.
- `clips.entities` projection updates from `NULL` to `'[]'`.
- Replaying the same event produces no second emission.

### 5.2 Redact passthrough

- Given a `scribe.clip.entities.v1` with empty entities, the plugin emits `scribe.clip.redacted.v1` with `redacted_transcript_ref == original_transcript_ref`, `passthrough: true`, `redactions: []`.
- `clips.redacted_transcript_ref` updates from `NULL` to the original's sha256.
- Replaying produces no second emission.
- Blob store contains exactly one transcript blob (no duplicate write).

### 5.3 Assemble with redacted source

- Given a session with three clips, each transcribed and redacted (passthrough), assemble runs and emits `scribe.session.assembled.v1` with `transcript_source: "redacted"`.
- The assembled context is byte-identical to what it would be reading from `clips.transcript`, because passthrough means identical refs.
- Flip `ASSEMBLE_TRANSCRIPT_SOURCE=original`, re-run, confirm same output (in passthrough — only differs in active redaction).

### 5.4 Degraded path

- Force NER to fail; confirm redact emits in degraded passthrough with `redactor.warnings: ["ner_unavailable"]`; confirm assemble still runs and produces output.

---

## 6. What to watch when CC ships this

Per your point about watching how new values attach:

- **Event names match exactly.** `scribe.clip.entities.v1`, `scribe.clip.ner_failed.v1`, `scribe.clip.redacted.v1`, `scribe.clip.redact_failed.v1`. No synonyms, no typos.
- **The four new event types are listed in specseed §6.** If they're not in the spec, they're not real.
- **Three new columns on `clips`** — `entities`, `redacted_transcript_ref`, `redactions` — added via migration, not by patching the existing schema in place.
- **`assemble-worker` reads via `redacted_transcript_ref` by default**, with env var override, and the source choice is stamped in the assembled event's data block.
- **No changes to `transcribe`, `structure`, `format`.** If any of those got modified, the additive property is broken and the PR needs a second look.
- **The original transcript is still readable from `clips.transcript` directly.** Easy to verify by querying SQLite after a 3-clip walkthrough.
- **In passthrough mode, original and redacted refs are equal.** Easy to verify in the open-session baggage debug view.
- **§15 of the spec has the rows for NER and redaction removed or rewritten.** The slice has moved from "later" to "v0 NOP."

If any of those drift, that's where to push back.

---

*End of plugin specs.*
