# `/transcribe-full` — endpoint spec for Unraid `faster-whisper`

**For:** `/mnt/user/Projects/fram-harness/docker/faster-whisper/serve.py`
**Why:** Clinical scribe `transcribe-worker` needs full-clip transcription with segments. The existing `POST /transcribe` is a head/tail slicer for bird-recording sampling and is not suitable. Adding a sibling endpoint preserves the existing flow.

## Status

**Deployed 2026-05-22** on `http://192.168.20.4:8765` (Unraid `faster-whisper` container — image rebuilt and recreated against this spec).

- `POST /transcribe-full` smoke test: 200 OK, response shape matches the contract below, `model=medium.en`, `device=cuda`, `compute_type=float16`, latency ~2.2 s on a 7 s clip after a cold model load.
- `POST /transcribe` regression: unchanged response shape and content for the same input.
- Error paths verified: `400 {"error":"missing audio part"}` and `400 {"error":"empty audio"}`.
- Container exposes an additional `MAX_AUDIO_MB` env (default `200`) governing the optional `413` response — not in the original spec, added as a guard.

## Contract

### Request

```
POST /transcribe-full
Content-Type: multipart/form-data
```

| Part | Required | Type | Notes |
|---|---|---|---|
| `audio` | yes | file (binary) | Audio bytes. Container ffmpeg-converts to 16kHz mono WAV internally; accept anything ffmpeg can read (webm/opus, mp4/aac, wav, mp3, flac). Max recommended 25 min. |
| `language` | no | text | ISO-639-1 hint (e.g. `en`). If omitted, whisper auto-detects. |
| `clip_id` | no | text | Echoed back in response for client correlation; not used internally. |

No JSON body. No `wav_path` — bytes only. The existing `/transcribe` is left untouched.

### Response — 200 OK

```json
{
  "clip_id": "<echoed if provided, else null>",
  "text": "<full transcript, single string>",
  "segments": [
    {
      "start": 0.0,
      "end": 4.32,
      "text": "Hi, what brings you in today?"
    }
  ],
  "language": "en",
  "language_probability": 0.997,
  "duration_s": 124.5,
  "model": "medium.en",
  "device": "cuda",
  "compute_type": "float16",
  "latency_ms": 8431
}
```

- `text` is the concatenation of `segments[].text` with single spaces. No leading/trailing whitespace.
- `segments[].start` / `end` in seconds from the start of the clip.
- `latency_ms` measures `model.transcribe` wall time only (not file decode / response serialization).

### Errors

| Status | Body | When |
|---|---|---|
| 400 | `{"error": "missing audio part"}` | No `audio` multipart part. |
| 400 | `{"error": "empty audio"}` | `audio` part is zero bytes. |
| 413 | `{"error": "audio too large", "limit_mb": N}` | (Optional) Reject files above a sensible cap, e.g. 200 MB. |
| 415 | `{"error": "ffmpeg decode failed", "detail": "<ffmpeg stderr tail>"}` | ffmpeg cannot decode. |
| 500 | `{"error": "transcription failed", "detail": "<exception class>"}` | Model invocation throws. |

Error responses never include stack traces.

## Implementation notes

- Reuse the existing `get_model()` / `_model_lock` / `unload_model()` / idle watcher in `serve.py` — no changes there. The new endpoint just acquires the model and calls `model.transcribe(<temp wav path>)`.
- Save the uploaded `audio` part to a `tempfile.NamedTemporaryFile(suffix=<original-ext-or-.bin>, delete=False)`, then ffmpeg-convert to 16 kHz mono WAV in a second temp file (same pattern as the existing `slice_audio` helper, just no slicing — single full output). Delete both in `finally:`.
- For `segments`: iterate `model.transcribe(...)` once (it returns a generator); materialise into a list and emit objects with only `start`, `end`, `text`. Strip whitespace from each segment text. Don't include `tokens`, `avg_logprob`, etc. — not needed by scribe v0.
- `info.duration` from `faster-whisper` is the model's view; prefer it over a separate ffprobe call.
- Use `time.perf_counter()` deltas around `model.transcribe` for `latency_ms`.
- `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` echoed into response from the existing env vars — single source of truth.

## What this does NOT do (kept for follow-ups)

- No diarization (`who_spoke`). Add later via pyannote or whisperX.
- No PHI redaction. Caller (scribe pipeline) handles via a separate stage.
- No streaming — single request → single response. Streaming partial transcripts is §15 future work.
- No model override per request. Container env-var `WHISPER_MODEL` remains authoritative. (Add `?model=large-v3` query param later if you want to A/B.)
- No auth. Container stays on the lan; scribe expects the existing trust boundary.

## Acceptance check

After deploy:

```bash
# From the Mac, with any small clip:
curl -s -F "audio=@/tmp/test.wav" -F "clip_id=test-001" \
  http://192.168.20.4:8765/transcribe-full | jq .
```

Pass if response shape matches the schema above, `text` is non-empty for a non-silent clip, and `model` echoes `"medium.en"`.

Existing `/transcribe` behaviour:

```bash
# Must still work after the change — bird-detection flow is unaffected.
curl -s -X POST http://192.168.20.4:8765/transcribe \
  -H "Content-Type: application/json" \
  -d '{"wav_path":"/mnt/user/field_Recording/some_existing.wav","duration_seconds":60}' | jq .
```
