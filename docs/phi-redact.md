# `/redact` — endpoint spec for Unraid `ductile-phi-detector`

**For:** `https://github.com/mattjoyce/ductile-phi-detector` (deployed by `/Volumes/Projects/unraid_admin/ductile-phi-detector/`)
**Why:** Clinical scribe needs to redact PHI (Protected Health Information) from each **Clip**'s transcript before persistence. The whisper-transcribe stage explicitly defers redaction to "a separate stage" (see `whisper-transcribe-full-spec.md` line 83). This is that stage. Model: [OpenMed-PII-SuperClinical-Small-44M-v1](https://huggingface.co/OpenMed/OpenMed-PII-SuperClinical-Small-44M-v1).

## Status

**Deployed 2026-05-23** on `http://192.168.20.4:8890` (Unraid `ductile-phi-detector` container).

- `POST /redact` smoke test: 200 OK, returns `{model, text, entities}` with whitespace-trimmed spans.
- `POST /detect` returns entities without redaction (same model, same shape).
- `GET /healthz` confirms the OpenMed model is loaded.
- `GET /v1/version` reports worker + transformers versions.
- Cold start: ~30 s on a fresh `/models` volume (first-request HF download). Steady-state: sub-second per typical Clip transcript.

## Where it sits in the scribe pipeline

```
Clip audio  ─▶  faster-whisper       ─▶  ductile-phi-detector  ─▶  scribea persist
                (192.168.20.4:8765)      (192.168.20.4:8890)        (sqlite + blobs)
                /transcribe-full          /redact
                returns transcript        returns redacted transcript
                                          + entity audit list
```

The redactor stands between the transcriber and persistence. **Original transcript text must never reach disk** — scribea stores the `text` field returned by `/redact` plus the `entities` audit list. The pre-redaction string lives only in the in-memory request buffer of whichever scribea worker bridges the two stages.

## Contract

### `POST /redact`

```
POST /redact
Content-Type: application/json
```

```jsonc
// request
{
  "text": "John Doe presented on 2026-01-15 with chest pain.",
  "placeholder_format": "[{label}]"   // optional; default "[{label}]"
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `text` | yes | string | The transcript to redact. Must be non-empty. No length cap enforced server-side, but practical ceiling ~2k tokens (BERT context). For long Clips, redact one segment at a time. |
| `placeholder_format` | no | string | Python `str.format` template with a `{label}` placeholder. Default `[{label}]` (e.g. `[first_name]`, `[date]`). Useful overrides: `<<{label}>>`, `***{label}***`, `«PHI:{label}»`. |

### Response — 200 OK

```jsonc
{
  "model": "OpenMed/OpenMed-PII-SuperClinical-Small-44M-v1",
  "text":  "[first_name] [last_name] presented on [date] with chest pain.",
  "entities": [
    { "start":  0, "end":  4, "label": "first_name", "score": 0.999, "text": "John" },
    { "start":  5, "end":  8, "label": "last_name",  "score": 0.999, "text": "Doe"  },
    { "start": 22, "end": 32, "label": "date",       "score": 0.999, "text": "2026-01-15" }
  ]
}
```

- `text` is the redacted output — **this is what scribea persists**. The field is named `text` (not `redacted`) to match the ductile event payload-name convention so a pipeline wrapper can pass it through unchanged.
- `entities` is the audit list: every PHI span the model found, with character offsets **into the original input text** (not into `text`), the model's label, its confidence, and the original substring. Persist this alongside the redacted text so an auditor can later prove what was removed without holding the original.
- `model` echoes the active OpenMed model id (set by the `OPENMED_MODEL` env var in the worker's docker-compose).
- Spans are whitespace-trimmed before redaction. The OpenMed tokenizer routinely emits spans like `" Doe"` (leading separator inside the span); the worker trims so adjacent placeholders don't eat the original spaces.

### Errors

| Status | Body | When |
|---|---|---|
| 400 | `{"detail": "text must be a non-empty string"}` | Missing or empty `text`. |
| 422 | (FastAPI default) | Malformed JSON or wrong field types. |
| 503 | `{"detail": "inference failed: <reason>"}` | Model raised mid-inference. Caller may retry; same malformed input will fail the same way. |
| 503 | `{"detail": "failed to load model '...': <reason>"}` | First-request model load failed (HF Hub unreachable, gated model without `HF_TOKEN`, disk full on `/models`). Retry after fixing the underlying cause. |

### `POST /detect` (no redaction, just the entity list)

Same request body shape as `/redact` (minus `placeholder_format`). Returns `{model, entities}`. Useful when scribea wants to highlight PHI in the UI without rewriting the transcript — e.g. a clinician-facing review pane where redaction is visual.

### `GET /healthz`

Forces model load on first call, then returns:

```jsonc
{ "status": "ok", "model": "OpenMed/...", "transformers_version": "5.9.0" }
```

Safe to call from a probe loop. Use as the readiness signal for any scribea worker that depends on the redactor.

### `GET /v1/version`

```jsonc
{ "name": "ductile-phi-detector", "version": "0.2.0", "model": "OpenMed/...", "transformers_version": "5.9.0" }
```

## Integration notes for scribea

### Where to call

Insert the call between `transcribe-worker`'s response and the SQLite write. Pseudocode for the worker that bridges the two stages:

```python
import httpx

PHI_REDACT_URL = "http://192.168.20.4:8890/redact"

async def redact(text: str) -> tuple[str, list[dict]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(PHI_REDACT_URL, json={"text": text})
        r.raise_for_status()
        body = r.json()
    return body["text"], body["entities"]
```

Call it once per Clip transcript (not per segment) so the model sees full sentence context. If a Clip's transcript exceeds ~2k tokens, split on sentence boundaries and stitch the redacted outputs back together.

### What to persist

| Field | Storage | Notes |
|---|---|---|
| `text` (redacted) | `clips.transcript` column | Replaces what would have been the raw transcript. |
| `entities` (audit) | `clips.phi_audit` column, JSON-typed | Lets future auditors verify the redaction without retaining the original. |
| `phi_model` | `clips.phi_model` column | Echo `body["model"]` so a future model swap is traceable in the audit trail. |
| `phi_redacted_at` | `clips.phi_redacted_at` column | ISO timestamp of the response receipt. |

**Do not persist the original transcript anywhere** — not in logs, not in stderr, not in `tempfile`s that outlive the request. The unredacted string is allowed to exist only in the in-memory variable from `model.transcribe()` return up to the `/redact` response receipt.

### Retry policy

- HTTP 5xx → retry once with a 2-second backoff. The model occasionally crashes on tokenizer edge cases (very long words, weird unicode); the second attempt usually succeeds.
- HTTP 4xx → fail the Clip with a structured error (`error_code: "redact_rejected"`). Don't retry — the input is the problem.
- Connection error / timeout → retry up to 3 times with exponential backoff. The unraid box is on the same LAN; sustained failures mean the container is down (check `docker ps` on 192.168.20.4).

### Label set (current model — Small-44M-v1)

Inspect the live `id2label` via the worker's startup logs or by calling `/detect` on a sample. As of v0.2.0 the model emits (non-exhaustive, snake_case):

`first_name`, `last_name`, `date`, `age`, `phone_number`, `email`, `street_address`, `city`, `state`, `zip_code`, `hospital`, `doctor`, `id_number`, `url`.

The exact set is the model's responsibility, not ours — scribea should treat unknown labels gracefully (display them as-is in the audit pane). If a labelling gap matters clinically (e.g. medication names leaking through), the right fix is upstream: swap to the Large-434M variant (needs GPU), fine-tune, or add a second redaction pass with a different model.

## What this does NOT do (kept for follow-ups)

- **No diarization-aware redaction.** Speaker labels are not consulted. A Clip with two speakers gets the same blanket redaction as a solo dictation.
- **No streaming.** Single request → single response. For very long Clips, scribea splits client-side.
- **No client-side per-clinician opt-outs.** Every Clip is redacted; if a clinician wants to keep their own name, that's a UI-layer un-redact pass against the `entities` audit list, not a request-time toggle.
- **No GPU acceleration yet.** Worker runs on CPU torch (Small-44M-v1). When GPU passthrough lands on the unraid box, swap to Large-434M-v1 via the `OPENMED_MODEL` env var + Dockerfile base image change.
- **No auth.** Same trust boundary as the whisper transcriber — both sit on the LAN and assume scribea is the only caller. If scribea ever leaves the LAN, both endpoints need a reverse proxy with auth.

## Acceptance check

After deploy:

```bash
# From the Mac, or from any scribea worker host on the LAN:
curl -s http://192.168.20.4:8890/healthz | jq .
# → { "status": "ok", "model": "OpenMed/...", "transformers_version": "5.9.0" }

curl -s -X POST http://192.168.20.4:8890/redact \
  -H "Content-Type: application/json" \
  -d '{"text": "Mrs Wilson (DOB 1958-03-14) admitted by Dr Chen via ED for sepsis."}' | jq .
# → text should have name + DOB + doctor name replaced by [label] placeholders
# → entities should list each removed span with start/end offsets into the input
```

Pass if `/redact` returns a `text` field where the originally-named entities are placeholders, and `entities` lists each removed span with the original substring intact for audit. Sub-second latency end-to-end on the LAN.

## Upstream references

- Worker repo: `https://github.com/mattjoyce/ductile-phi-detector` (private)
- Worker README: `/Volumes/Projects/ductile-phi-detector/README.md`
- Operator (deploy) scripts: `/Volumes/Projects/unraid_admin/ductile-phi-detector/`
- Optional gateway-side ductile plugin (not currently wired into a pipeline; scribea calls the HTTP endpoint directly): `/Volumes/Projects/ductile-plugins/phi_detect/`
- Model card: `https://huggingface.co/OpenMed/OpenMed-PII-SuperClinical-Small-44M-v1`
