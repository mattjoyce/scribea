# scribe-audio-preprocess — plugin spec

**Status:** Draft v0
**Parent:** [`specseed.md`](./specseed.md)
**Scope:** v0 plugin contract for the audio preprocessing stage that sits between ingest and transcribe.

---

## 1. Purpose

The `scribe-audio-preprocess` plugin does three jobs on every clip:

1. **Canonicalize** the audio to a known format (mono, 16 kHz, 16-bit PCM, WAV) so every downstream stage can stop caring about what the browser delivered.
2. **Filter** out low-frequency content below 80 Hz (HVAC rumble, table thumps, breath puffs) that adds nothing to speech and degrades downstream analysis.
3. **Measure** the audio quality of both the raw and processed signal, stamping the results into baggage as a user-facing verdict and a machine-readable metric block.

The plugin is the first stage where the system's principles meet the messy reality of phone microphones and clinical environments. It is also the stage that earns the audit trail's right to claim it knows what the system actually heard.

---

## 2. Position in the pipeline

```
ingest → audio-preprocess → transcribe → assemble → structure → format
```

This is a per-clip stage. It runs once per clip, in parallel across clips within a session.

---

## 3. Inputs and outputs

### 3.1 Subscribes to

| Event                          | Payload                                                                 |
|--------------------------------|-------------------------------------------------------------------------|
| `scribe.clip.received.v1`      | `{ audio_ref, seq, started_at, duration_ms }`                           |

### 3.2 Emits

| Event                                  | Payload                                                                                                                                                                                                                |
|----------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `scribe.clip.preprocessed.v1`          | `{ audio_ref: <new>, original_audio_ref, quality: { raw, processed, verdict }, preprocessing: { ... } }`                                                                                                              |
| `scribe.clip.preprocess_failed.v1`     | `{ original_audio_ref, reason, stage }`                                                                                                                                                                                |

The `audio_ref` in the success event is the **new** content-addressed blob (the cleaned WAV). The `original_audio_ref` is preserved so the audit trail and any future A/B work can reach both. Both blobs persist; nothing is deleted.

---

## 4. Canonicalization

The output format is fixed:

| Property         | Value                              |
|------------------|------------------------------------|
| Container        | WAV (RIFF)                         |
| Codec            | PCM (uncompressed)                 |
| Channels         | 1 (mono)                           |
| Sample rate      | 16 000 Hz                          |
| Sample format    | 16-bit signed integer (`s16le`)    |

Stereo input is summed to mono (channel average), not L-only — averaging preserves voice energy when both mics picked it up.

Resampling uses ffmpeg's default `swr` resampler at default quality. For POC this is fine; if artefacts appear in spectrograms later, switch to `soxr` (`-af aresample=resampler=soxr`).

No re-encoding to any lossy codec at any point. Once the original Opus/AAC has been decoded, the signal stays PCM through every subsequent stage.

---

## 5. High-pass filter

A 1st-order Butterworth high-pass at 80 Hz (`-af highpass=f=80`). Removes HVAC rumble, footsteps, breath onsets, table noise. Speech fundamentals (~85 Hz adult male, higher for female) are minimally affected; intelligibility is preserved.

This is deliberately conservative for POC. Aggressive filtering or noise reduction (RNNoise, DeepFilterNet, spectral subtraction) is out of scope — those can hurt Whisper's WER as often as they help, and the place to learn that is on real recordings via the quality metrics, not by pre-emptive denoising.

---

## 6. The ffmpeg invocation

A single command does the whole canonicalization + filter:

```
ffmpeg -hide_banner -loglevel error \
  -i <input> \
  -ac 1 \
  -ar 16000 \
  -sample_fmt s16 \
  -af highpass=f=80 \
  <output>.wav
```

`-hide_banner -loglevel error` keeps logs clean — only errors appear, which is what supervision wants to see. The output is then content-addressed (sha256) and stored in the blob store.

---

## 7. Quality measurement

Quality is measured **twice**: once on the original signal (pre-filter, decoded to PCM in memory), once on the cleaned signal (post-filter, the WAV that goes downstream).

The `raw` block describes the recording environment. It is user-actionable: tells the clinician whether the room/mic/situation was OK.

The `processed` block describes what Whisper actually sees. It is pipeline-actionable: tells the engineer whether the cleaned signal is good enough to transcribe well, and lets us learn whether the HPF earned its keep across many sessions.

If the two diverge meaningfully (raw SNR poor, processed SNR good), the preprocessing pulled its weight. If they're identical (the HPF made no difference), the noise wasn't in the low band — useful signal for what to add next.

### 7.1 Metrics

Four numbers per block. All cheap, all standard.

| Metric                    | Definition                                                                                                                                                          | Units    |
|---------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------|
| `clipping_ratio`          | Fraction of samples where `abs(sample) >= 0.99` (after normalizing s16 to float in [-1, 1]).                                                                        | ratio    |
| `rms_dbfs`                | `20 * log10(rms(samples) / 1.0)` for float samples in [-1, 1]. Silent clip (rms == 0) reported as `-inf` or capped at `-120`.                                       | dBFS     |
| `snr_estimate_db`         | Ratio of energy in voiced frames to energy in unvoiced frames, expressed in dB. Frames classified via Silero VAD. Equivalent to a poor-man's segmental SNR.         | dB       |
| `speech_presence_ratio`   | Fraction of 30 ms frames classified as speech by Silero VAD.                                                                                                        | ratio    |

WADA-SNR (Kim & Stern 2008) is the textbook non-intrusive estimator and is a reasonable post-POC upgrade if the VAD-based SNR proves too coarse. For v0, the VAD-based estimator is enough and uses a library you'll want for other reasons (silence trimming, diarization later).

### 7.2 Frame parameters for VAD

| Parameter      | Value      |
|----------------|------------|
| Frame size     | 30 ms      |
| Hop            | 30 ms (non-overlapping) |
| VAD model      | Silero VAD (v4.0 or current) via `torch.hub` |
| VAD threshold  | 0.5 (default)                                |

Silero is chosen for: small (~2 MB), fast (real-time on CPU), no external service, MIT licensed. The same VAD instance can be reused for speech presence ratio and for snr_estimate.

---

## 8. Verdict

Server computes one categorical verdict per block from the four metrics, so the PWA can render an indicator without re-implementing thresholds. Verdict is in priority order: the first matching rule wins.

| Verdict     | Rule                                                                                                                       |
|-------------|----------------------------------------------------------------------------------------------------------------------------|
| `clipped`   | `clipping_ratio > 0.001` (more than 0.1% of samples at limits)                                                             |
| `silent`    | `speech_presence_ratio < 0.05`                                                                                             |
| `quiet`     | `rms_dbfs < -45`                                                                                                           |
| `noisy`     | `snr_estimate_db < 15`                                                                                                     |
| `clean`     | none of the above                                                                                                          |

Thresholds are POC guesses, not science. They are configurable via the plugin's environment so they can be tuned without a rebuild:

| Env var                                  | Default |
|------------------------------------------|---------|
| `PREPROCESS_THRESHOLD_CLIPPING_RATIO`    | 0.001   |
| `PREPROCESS_THRESHOLD_SILENT_PRESENCE`   | 0.05    |
| `PREPROCESS_THRESHOLD_QUIET_DBFS`        | -45     |
| `PREPROCESS_THRESHOLD_NOISY_SNR_DB`      | 15      |

The plugin stamps the threshold values it used into `preprocessing.thresholds` (see §9) so the audit trail is self-describing — a verdict from last month can be re-evaluated against current thresholds without ambiguity.

The composite session-level verdict (e.g., "1 noisy clip, 2 clean") is computed by the assemble stage at session-close, not here. This plugin produces per-clip verdicts only.

---

## 9. Baggage schema

The event's `data` block:

```json
{
  "audio_ref": "sha256:b3f...",
  "original_audio_ref": "sha256:a1e...",
  "quality": {
    "raw": {
      "clipping_ratio": 0.00012,
      "rms_dbfs": -22.4,
      "snr_estimate_db": 11.8,
      "speech_presence_ratio": 0.62,
      "verdict": "noisy"
    },
    "processed": {
      "clipping_ratio": 0.00012,
      "rms_dbfs": -23.1,
      "snr_estimate_db": 19.4,
      "speech_presence_ratio": 0.64,
      "verdict": "clean"
    }
  },
  "preprocessing": {
    "tool": "ffmpeg",
    "tool_version": "6.1.1",
    "input_format": "audio/webm; codecs=opus",
    "input_sample_rate": 48000,
    "input_channels": 2,
    "input_duration_ms": 8420,
    "input_bytes": 67234,
    "output_format": "audio/wav",
    "output_sample_rate": 16000,
    "output_channels": 1,
    "output_bit_depth": 16,
    "output_duration_ms": 8420,
    "output_bytes": 269440,
    "filters": ["highpass=f=80"],
    "vad_model": "silero-vad-v4.0",
    "thresholds": {
      "clipping_ratio": 0.001,
      "silent_presence": 0.05,
      "quiet_dbfs": -45,
      "noisy_snr_db": 15
    }
  }
}
```

The event's `meta` block (per the universal baggage envelope in specseed §4.4) carries the plugin's standard observability:

```json
{
  "worker": "scribe-audio-preprocess",
  "worker_version": "0.1.0",
  "model": null,
  "latency_ms": 142,
  "cost_usd": 0.0,
  "tokens_in": 0,
  "tokens_out": 0,
  "node": "macbook-mjoyce"
}
```

---

## 10. Failure modes

The plugin fails loudly per the Armstrong principle. It does not attempt fallback transcription on raw audio if preprocessing fails — that path bypasses the canonicalization contract and complicates the audit story without earning much. A failed preprocess is a failed clip; assemble notes the gap at session-close.

| Failure                                      | Response                                                                                                                            |
|----------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| ffmpeg exit non-zero                         | Emit `scribe.clip.preprocess_failed.v1` with `reason: "ffmpeg_failed"`, `stage: "canonicalize"`, stderr in meta.                    |
| Input shorter than 100 ms after decode       | Emit failed with `reason: "too_short"`. Probably the user pressed start/stop accidentally.                                          |
| Input longer than 600 s (10 min) after decode | Process normally but stamp `preprocessing.warnings: ["over_max_clip_duration"]`. Don't drop the audio.                              |
| VAD model load fails                         | Process the audio (canonicalize + HPF still happen), skip the VAD-derived metrics, stamp `preprocessing.warnings: ["vad_unavailable"]`, emit success. Quality block reports `snr_estimate_db: null, speech_presence_ratio: null, verdict: "unknown"`. |
| Blob store write fails                       | Emit failed with `reason: "blob_write_failed"`. Likely supervisor-level (disk full, permissions) — let Ductile retry per policy.    |

The "VAD optional, ffmpeg required" split reflects what's load-bearing for v0: canonicalization is the contract, quality measurement is a bonus.

---

## 11. Idempotency

The plugin is idempotent against `event_id`. On handling a `scribe.clip.received.v1`:

1. Check `scribe.db.events` for an existing `scribe.clip.preprocessed.v1` (or failed) row with the same `clip_id`. If present, no-op.
2. Compute sha256 of the cleaned WAV before writing the blob. If the blob already exists (content-addressed dedup), reuse it.
3. Write the projection row to `clips` only if the clip's state is not already `preprocessed` or `failed`.

Replay of the same event must produce the same downstream effect or be safely deduplicated. This holds because the canonicalization is deterministic given the same input bytes and ffmpeg version (within minor float-rounding tolerance, which doesn't affect the sha256 in practice — same bytes in, same bytes out).

---

## 12. Stub mode

Unlike `scribe-transcribe` (which needs the Unraid GPU container) and `scribe-structure` (which needs an API key), this plugin has no external dependencies beyond ffmpeg, numpy, soundfile, and torch+silero — all installable in the local Python environment. There is no honest-gates stub mode for v0; if ffmpeg or torch is missing, the plugin fails loudly at startup and refuses to consume events.

If a future operational mode needs a passthrough (e.g., for benchmark comparisons), a `PREPROCESS_PASSTHROUGH=1` env var would copy the input blob to a new `audio_ref` unchanged and emit `preprocessing.tool: "passthrough"` in baggage. Not in scope for v0.

---

## 13. Implementation notes

### 13.1 Stack

- Python 3.11+
- `ffmpeg` 6.x in PATH (subprocess)
- `numpy`, `soundfile` for sample-level math and WAV I/O
- `torch` + `silero-vad` for VAD
- Optional: `wada-snr` package for the WADA estimator, post-POC

### 13.2 Layout

Matches the existing `plugins/scribe-*` convention:

```
plugins/scribe-audio-preprocess/
  pyproject.toml
  scribe_audio_preprocess/
    __init__.py
    __main__.py            # Ductile entry point
    canonicalize.py        # ffmpeg subprocess wrapper
    quality.py             # metric computation
    verdict.py             # threshold logic
    vad.py                 # Silero loading + frame classification
  tests/
    test_canonicalize.py
    test_quality.py
    test_verdict.py
    fixtures/
      clean_speech.wav
      noisy_room.wav
      clipped.wav
      silent.wav
```

### 13.3 Environment

| Env var                       | Required | Default   | Notes                                              |
|-------------------------------|----------|-----------|----------------------------------------------------|
| `BLOBS_DIR`                   | yes      | -         | shared with ingress and other plugins              |
| `FFMPEG_BIN`                  | no       | `ffmpeg`  | override for non-PATH install                      |
| `VAD_DEVICE`                  | no       | `cpu`     | `cpu` or `cuda`; CPU is fine for clip-sized audio  |
| `PREPROCESS_THRESHOLD_*`      | no       | see §8    | tune verdicts without rebuild                      |
| `PREPROCESS_MAX_CLIP_SECONDS` | no       | `600`     | warning threshold, not a hard cap                  |

### 13.4 Latency budget

Target on commodity hardware (M-series Mac or modest x86):

| Operation                              | Expected latency for a 60s clip |
|----------------------------------------|---------------------------------|
| ffmpeg canonicalize + HPF              | < 200 ms                        |
| Read WAV + compute clipping_ratio, rms | < 50 ms                         |
| Silero VAD pass                        | < 300 ms (CPU)                  |
| Verdict + write event                  | < 10 ms                         |
| **Total**                              | < 600 ms per clip               |

If the plugin exceeds 2 s on a 60 s clip, something is wrong (probably torch loading the VAD model per-invocation rather than once per process).

---

## 14. Testing

Per the specseed §13 approach: deterministic replay via fixtures.

| Fixture                | Expected verdict (raw) | Notes                                                          |
|------------------------|------------------------|----------------------------------------------------------------|
| `clean_speech.wav`     | `clean`                | studio-quality speech                                          |
| `noisy_room.wav`       | `noisy`                | speech + ambient ~ -10 dB SNR                                  |
| `clipped.wav`          | `clipped`              | recording with > 1% sample clipping                            |
| `silent.wav`           | `silent`               | room tone only, no speech                                      |
| `quiet_speech.wav`     | `quiet`                | normal speech recorded at very low gain (-50 dBFS RMS)         |
| `washing_machine.wav`  | `noisy`                | the canonical POC fixture: record next to a washing machine    |

The washing-machine fixture is the one this plugin exists to make legible. The test asserts not just the verdict but the divergence between raw and processed SNR — that's what tells us the HPF earned its keep on low-frequency mechanical noise.

The plugin is otherwise tested via the standard event-replay harness from specseed §13.

---

## 15. Open questions

1. **VAD model pinning.** Silero ships periodic updates that can shift speech-presence-ratio noticeably. Pin to a specific commit hash via `torch.hub` rather than `master`, and bump deliberately.
2. **Sample-rate aware HPF.** A 1st-order Butterworth at 80 Hz is fine at 16 kHz; if we ever support higher sample rates the filter coefficient calculation needs to be sample-rate-relative. Out of scope for v0.
3. **Should the verdict be a single value or per-block?** Currently per-block (raw and processed each have a verdict). The session-level roll-up will need a strategy for combining them. Probably "use the processed verdict for clinician feedback, use both for engineering analysis."
4. **PWA polling vs SSE for verdict.** The verdict ideally lands in the active-session UI within a second of a clip stopping. SSE on `/sessions/{id}/live` is the cleaner path; polling `/sessions/{id}/clips` every 2 s is the fallback. Decided in the PWA spec, not here.

---

*End of plugin spec.*
