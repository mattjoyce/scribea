#!/usr/bin/env python3
"""scribe-audio-preprocess: canonicalize one clip to 16 kHz mono s16le WAV
with an 80 Hz high-pass filter, sha256-address the cleaned WAV in the blob
store, and emit scribe.clip.preprocessed.v1 so the transcribe step picks up
the cleaned blob.

Step 1 scope (see docs/scribe-audio-preprocess.md): no quality metrics, no
VAD, no verdicts. Just the canonicalize → HPF → content-address → emit chain.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    baggage, err, ingress_callback, ok, read_request, with_timer, write_response,
)

WORKER = "scribe-audio-preprocess"
VERSION = "0.1.0"


def ffmpeg_version(ffmpeg_bin: str) -> str:
    """Run `ffmpeg -version`, return the first line (e.g. 'ffmpeg version 8.1 …')."""
    try:
        out = subprocess.run(
            [ffmpeg_bin, "-version"], capture_output=True, timeout=5, text=True,
        )
        return (out.stdout or "").splitlines()[0].strip() if out.stdout else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def canonicalize(ffmpeg_bin: str, input_path: str, output_path: str,
                 timeout: float = 60.0) -> tuple[bool, str]:
    """Run the §6 ffmpeg invocation. Returns (success, stderr_tail)."""
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path,
        "-ac", "1",
        "-ar", "16000",
        "-sample_fmt", "s16",
        "-af", "highpass=f=80",
        output_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    except FileNotFoundError as e:
        return False, f"ffmpeg not found: {e}"
    if res.returncode != 0:
        return False, (res.stderr or "")[-500:]
    return True, ""


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

    # ffmpeg writes to a temp file; we sha256 it, then move to blobs_dir.
    tmp_fd, tmp_out = tempfile.mkstemp(prefix=".preprocess-", suffix=".wav", dir=blobs_dir)
    os.close(tmp_fd)
    try:
        success, stderr_tail = canonicalize(ffmpeg_bin, blob_path, tmp_out, timeout=timeout)
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
    finally:
        if tmp_out and os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass

    new_audio_ref = "sha256:" + sum_hex

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
        # Quality block intentionally absent for step 1 — see
        # docs/scribe-audio-preprocess.md §7 for the planned schema.
    }

    meta = baggage(WORKER, VERSION, latency_ms=elapsed(),
                   extra={"filters": preprocessing["filters"],
                          "output_audio_ref": new_audio_ref})

    try:
        ingress_callback(ingress_url, f"/internal/clips/{clip_id}/preprocessed", {
            "session_id": session_id,
            "audio_ref": new_audio_ref,
            "original_audio_ref": original_audio_ref,
            "blob_path": dest,
            "preprocessing": preprocessing,
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
            },
        }],
        logs=[{"level": "info",
               "message": f"clip {clip_id} preprocessed (filters={preprocessing['filters']})"}],
    ))


if __name__ == "__main__":
    main()
