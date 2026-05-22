#!/usr/bin/env python3
"""scribe-audio-preprocess: canonicalize one clip to 16 kHz mono s16le WAV
with an 80 Hz high-pass filter, sha256-address the cleaned WAV in the blob
store, measure quality on both the raw and processed signals, and emit
scribe.clip.preprocessed.v1 so the transcribe step picks up the cleaned blob.

Step 2 scope (see docs/scribe-audio-preprocess.md): quality metrics now
include `clipping_ratio` and `rms_dbfs` per block. Silero VAD-derived metrics
(`snr_estimate_db`, `speech_presence_ratio`) remain null until step 3;
verdicts gate on what's measurable — `clipped` and `quiet` work without VAD,
otherwise the verdict is `unknown`. `preprocessing.warnings` carries
`vad_unavailable` so the audit trail is honest about what was measured.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    baggage, err, ingress_callback, ok, read_request, with_timer, write_response,
)

WORKER = "scribe-audio-preprocess"
VERSION = "0.2.0"

# Verdict thresholds — overridable via env per spec §8.
THRESHOLD_CLIPPING_RATIO = float(os.environ.get("PREPROCESS_THRESHOLD_CLIPPING_RATIO", "0.001"))
THRESHOLD_SILENT_PRESENCE = float(os.environ.get("PREPROCESS_THRESHOLD_SILENT_PRESENCE", "0.05"))
THRESHOLD_QUIET_DBFS = float(os.environ.get("PREPROCESS_THRESHOLD_QUIET_DBFS", "-45"))
THRESHOLD_NOISY_SNR_DB = float(os.environ.get("PREPROCESS_THRESHOLD_NOISY_SNR_DB", "15"))


def ffmpeg_version(ffmpeg_bin: str) -> str:
    """Run `ffmpeg -version`, return the first line (e.g. 'ffmpeg version 8.1 …')."""
    try:
        out = subprocess.run(
            [ffmpeg_bin, "-version"], capture_output=True, timeout=5, text=True,
        )
        return (out.stdout or "").splitlines()[0].strip() if out.stdout else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def ffmpeg_pcm(ffmpeg_bin: str, input_path: str, output_path: str,
               apply_hpf: bool, timeout: float = 60.0) -> tuple[bool, str]:
    """Decode input to 16 kHz mono s16le WAV. When apply_hpf=True the §6
    invocation is used (highpass=f=80); when False the same shape is produced
    without the filter so quality.raw can be measured at the same sample rate
    as quality.processed (isolating the HPF as the only variable)."""
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path,
        "-ac", "1",
        "-ar", "16000",
        "-sample_fmt", "s16",
    ]
    if apply_hpf:
        cmd += ["-af", "highpass=f=80"]
    cmd += [output_path]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    except FileNotFoundError as e:
        return False, f"ffmpeg not found: {e}"
    if res.returncode != 0:
        return False, (res.stderr or "")[-500:]
    return True, ""


def read_mono_s16(path: str) -> tuple[list[int], int, int]:
    """Read a mono 16-bit PCM WAV. Returns (samples, sample_rate, n_samples)."""
    with wave.open(path, "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        if ch != 1 or sw != 2:
            raise ValueError(f"expected mono s16, got channels={ch} sampwidth={sw}")
        n = wf.getnframes()
        frames = wf.readframes(n)
    if n == 0:
        return [], sr, 0
    samples = list(struct.unpack("<" + "h" * n, frames))
    return samples, sr, n


def quality_block(samples: list[int]) -> dict:
    """Compute the §7.1 quality block from mono s16le PCM samples.

    Without VAD we set snr_estimate_db and speech_presence_ratio to None and
    let verdict_for() short-circuit to `unknown` unless clipping/quiet trip
    first. Verdict priority matches spec §8.
    """
    n = len(samples)
    if n == 0:
        return {
            "clipping_ratio": 0.0,
            "rms_dbfs": -120.0,
            "snr_estimate_db": None,
            "speech_presence_ratio": None,
            "verdict": "silent",
        }
    # clipping_ratio: |sample| >= 0.99 of full scale
    threshold = int(0.99 * 32767)
    clipped = sum(1 for s in samples if -s >= threshold or s >= threshold)
    clipping_ratio = clipped / n
    # rms_dbfs: 20·log10(rms / 32768) — rms taken over float-normalised samples
    sumsq = 0
    for s in samples:
        sumsq += s * s
    mean_sq = sumsq / n
    rms_int = math.sqrt(mean_sq)
    if rms_int <= 0:
        rms_dbfs = -120.0
    else:
        rms_dbfs = 20.0 * math.log10(rms_int / 32768.0)
        if rms_dbfs < -120.0:
            rms_dbfs = -120.0
    return {
        "clipping_ratio": round(clipping_ratio, 6),
        "rms_dbfs": round(rms_dbfs, 2),
        "snr_estimate_db": None,
        "speech_presence_ratio": None,
        "verdict": verdict_for(clipping_ratio, rms_dbfs, None, None),
    }


def verdict_for(clipping_ratio: float, rms_dbfs: float,
                presence: float | None, snr: float | None) -> str:
    """Spec §8 priority order. VAD-dependent verdicts (silent, noisy) only
    fire if presence/snr are populated; when VAD is unavailable, fall through
    to `unknown` rather than overclaiming `clean`."""
    if clipping_ratio > THRESHOLD_CLIPPING_RATIO:
        return "clipped"
    if presence is not None and presence < THRESHOLD_SILENT_PRESENCE:
        return "silent"
    if rms_dbfs < THRESHOLD_QUIET_DBFS:
        return "quiet"
    if snr is not None and snr < THRESHOLD_NOISY_SNR_DB:
        return "noisy"
    if presence is None or snr is None:
        return "unknown"
    return "clean"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    req = read_request()
    cmd = req.get("command")

    if cmd == "health":
        write_response(ok("scribe-audio-preprocess alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")
    blobs_dir = cfg.get("blobs_dir")
    ffmpeg_bin = cfg.get("ffmpeg_bin") or "ffmpeg"
    timeout = float(cfg.get("request_timeout_seconds", 60))

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    clip_id = payload.get("clip_id")
    blob_path = payload.get("blob_path")
    original_audio_ref = payload.get("audio_ref")

    if not (session_id and clip_id and blob_path and original_audio_ref):
        write_response(err(
            "missing session_id/clip_id/blob_path/audio_ref in payload",
            retry=False,
        ))
        return
    if not blobs_dir:
        write_response(err("blobs_dir not configured", retry=False))
        return
    if not os.path.isfile(blob_path):
        write_response(err(f"input blob not found: {blob_path}", retry=False))
        return

    elapsed = with_timer()

    # ffmpeg writes the cleaned WAV to a temp file; we sha256 it, then move
    # to blobs_dir. The raw-decoded WAV (no HPF) is written next to it for
    # quality measurement and removed after we read it.
    tmp_fd, tmp_out = tempfile.mkstemp(prefix=".preprocess-", suffix=".wav", dir=blobs_dir)
    os.close(tmp_fd)
    raw_tmp_fd, raw_tmp = tempfile.mkstemp(prefix=".preprocess-raw-", suffix=".wav", dir=blobs_dir)
    os.close(raw_tmp_fd)
    try:
        success, stderr_tail = ffmpeg_pcm(ffmpeg_bin, blob_path, tmp_out,
                                          apply_hpf=True, timeout=timeout)
        if not success:
            # Audit + emit a failure event so assemble can note the gap.
            reason = f"ffmpeg_failed: {stderr_tail}".strip()
            try:
                ingress_callback(ingress_url, f"/internal/clips/{clip_id}/preprocess_failed", {
                    "session_id": session_id,
                    "original_audio_ref": original_audio_ref,
                    "reason": reason,
                    "stage": "canonicalize",
                    "meta": baggage(WORKER, VERSION, latency_ms=elapsed(),
                                    extra={"stage": "canonicalize"}),
                })
            except Exception:  # noqa: BLE001
                pass
            write_response(ok(
                "preprocess failed — emitted clip.preprocess_failed.v1",
                events=[{
                    "type": "scribe.clip.preprocess_failed.v1",
                    "payload": {
                        "session_id": session_id,
                        "clip_id": clip_id,
                        "original_audio_ref": original_audio_ref,
                        "reason": reason,
                        "stage": "canonicalize",
                    },
                }],
                logs=[{"level": "warn", "message": reason}],
            ))
            return

        sum_hex = sha256_file(tmp_out)
        dest = os.path.join(blobs_dir, sum_hex)
        if not os.path.exists(dest):
            os.rename(tmp_out, dest)
        else:
            os.remove(tmp_out)
        tmp_out = None  # don't unlink in finally

        # Quality measurement: decode the original (no HPF) to s16 mono 16 k
        # so raw and processed are at the same sample rate — the only
        # difference between them is the HPF.
        warnings: list[str] = ["vad_unavailable"]
        raw_quality: dict | None = None
        processed_quality: dict | None = None
        raw_success, raw_err = ffmpeg_pcm(ffmpeg_bin, blob_path, raw_tmp,
                                          apply_hpf=False, timeout=timeout)
        if raw_success:
            try:
                raw_samples, _, _ = read_mono_s16(raw_tmp)
                raw_quality = quality_block(raw_samples)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"raw_quality_failed:{type(e).__name__}")
        else:
            warnings.append(f"raw_decode_failed:{raw_err[:80]}")
        try:
            processed_samples, _, _ = read_mono_s16(dest)
            processed_quality = quality_block(processed_samples)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"processed_quality_failed:{type(e).__name__}")
    finally:
        if tmp_out and os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        if raw_tmp and os.path.exists(raw_tmp):
            try: os.remove(raw_tmp)
            except Exception: pass

    new_audio_ref = "sha256:" + sum_hex

    quality_payload = {
        "raw": raw_quality,
        "processed": processed_quality,
    }

    preprocessing = {
        "tool": "ffmpeg",
        "tool_version": ffmpeg_version(ffmpeg_bin),
        "filters": ["highpass=f=80"],
        "output_format": "audio/wav",
        "output_sample_rate": 16000,
        "output_channels": 1,
        "output_bit_depth": 16,
        "output_audio_ref": new_audio_ref,
        "output_blob_path": dest,
        "output_bytes": os.path.getsize(dest),
        "vad_model": None,  # populated when step 3 adds Silero
        "thresholds": {
            "clipping_ratio": THRESHOLD_CLIPPING_RATIO,
            "silent_presence": THRESHOLD_SILENT_PRESENCE,
            "quiet_dbfs": THRESHOLD_QUIET_DBFS,
            "noisy_snr_db": THRESHOLD_NOISY_SNR_DB,
        },
        "warnings": warnings,
    }

    meta = baggage(WORKER, VERSION, latency_ms=elapsed(),
                   extra={"filters": preprocessing["filters"],
                          "output_audio_ref": new_audio_ref,
                          "verdict_raw": (raw_quality or {}).get("verdict"),
                          "verdict_processed": (processed_quality or {}).get("verdict")})

    try:
        ingress_callback(ingress_url, f"/internal/clips/{clip_id}/preprocessed", {
            "session_id": session_id,
            "audio_ref": new_audio_ref,
            "original_audio_ref": original_audio_ref,
            "blob_path": dest,
            "preprocessing": preprocessing,
            "quality": quality_payload,
            "meta": meta,
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return

    write_response(ok(
        f"preprocessed clip {clip_id} -> {new_audio_ref[:14]}…",
        events=[{
            "type": "scribe.clip.preprocessed.v1",
            "payload": {
                "session_id": session_id,
                "clip_id": clip_id,
                "audio_ref": new_audio_ref,
                "original_audio_ref": original_audio_ref,
                "blob_path": dest,
                "audio_format": "audio/wav",
                "preprocessing": preprocessing,
                "quality": quality_payload,
            },
        }],
        logs=[{"level": "info",
               "message": f"clip {clip_id} preprocessed "
                          f"(raw={(raw_quality or {}).get('verdict')}, "
                          f"processed={(processed_quality or {}).get('verdict')})"}],
    ))


if __name__ == "__main__":
    main()
