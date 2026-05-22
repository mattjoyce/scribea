#!/usr/bin/env python3
"""scribe-transcribe: send clip audio to the configured whisper service.

If `whisper_url` is unset, unreachable, or `stub_mode=true` is configured,
the plugin returns a canned transcript stamped with `stt_model=stub` so the
audit trail stays honest about what produced the text.
"""
from __future__ import annotations

import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid as _uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ok, read_request, write_response,
)

WORKER = "scribe-transcribe"
VERSION = "0.1.0"


def _build_multipart(file_path: str, audio_format: str, clip_id: str) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body without pulling in requests."""
    boundary = "----scribe-" + _uuid.uuid4().hex
    crlf = b"\r\n"
    ext = mimetypes.guess_extension(audio_format or "") or ".bin"
    filename = f"{clip_id}{ext}"
    with open(file_path, "rb") as f:
        audio_bytes = f.read()
    parts: list[bytes] = []
    # clip_id text part
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(b'Content-Disposition: form-data; name="clip_id"' + crlf + crlf)
    parts.append(clip_id.encode() + crlf)
    # audio file part
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(
        ('Content-Disposition: form-data; name="audio"; filename="' + filename + '"').encode()
        + crlf
    )
    parts.append(("Content-Type: " + (audio_format or "application/octet-stream")).encode() + crlf + crlf)
    parts.append(audio_bytes + crlf)
    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _post_whisper(whisper_url: str, body: bytes, content_type: str, timeout: float) -> dict:
    req = urllib.request.Request(
        whisper_url.rstrip("/") + "/transcribe-full",
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        import json as _json
        return _json.loads(resp.read().decode("utf-8"))


def main() -> None:
    req = read_request()
    cmd = req.get("command")
    if cmd == "health":
        write_response(ok("scribe-transcribe alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")
    whisper_url = cfg.get("whisper_url") or os.environ.get("WHISPER_URL", "")
    stub_mode = str(cfg.get("stub_mode", "false")).lower() == "true"
    timeout = float(cfg.get("request_timeout_seconds", 120))

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    clip_id = payload.get("clip_id")
    blob_path = payload.get("blob_path")
    audio_format = payload.get("audio_format") or "audio/webm"

    if not (session_id and clip_id and blob_path):
        write_response(err("missing session_id/clip_id/blob_path in payload", retry=False))
        return

    sw = Stopwatch()
    transcript_text = ""
    segments: list[dict] = []
    model = "stub"

    # If the whisper service isn't configured or stub mode is forced, return
    # a placeholder transcript. Audit trail records stt_model=stub honestly.
    if stub_mode or not whisper_url:
        transcript_text = f"[stub transcript for clip {clip_id} — wire WHISPER_URL to get a real one]"
        segments = [{"start": 0.0, "end": 1.0, "text": transcript_text}]
        sw.mark("stub_ms")
    else:
        try:
            body, content_type = _build_multipart(blob_path, audio_format, clip_id)
            sw.mark("multipart_build_ms")
            resp = _post_whisper(whisper_url, body, content_type, timeout)
            sw.mark("whisper_http_ms")
            transcript_text = (resp.get("text") or "").strip()
            segments = resp.get("segments") or []
            model = resp.get("model") or "unknown"
        except urllib.error.URLError as e:
            # Network or service down — emit clip.failed.v1 and callback ingress so the
            # session sweeper / assembly path can move on with a gap.
            reason = f"whisper unreachable: {e}"
            meta = baggage(WORKER, VERSION, latency_ms=sw.total_ms(),
                           model="error", timings=sw.phases,
                           extra={"stt_model": "error", "audio_format": audio_format})
            try:
                ingress_callback(ingress_url, f"/internal/clips/{clip_id}/failed", {
                    "session_id": session_id,
                    "reason": reason,
                    "meta": meta,
                })
            except Exception as cb_err:
                # Best-effort; still emit a Ductile event.
                pass
            write_response(ok(
                "transcribe failed — emitted clip.failed.v1",
                events=[{
                    "type": "scribe.clip.failed.v1",
                    "payload": {"session_id": session_id, "clip_id": clip_id, "reason": reason},
                }],
                logs=[{"level": "warn", "message": reason}],
            ))
            return
        except Exception as e:  # noqa: BLE001
            write_response(err(f"transcribe error: {e}", retry=True))
            return

    # Persist back to scribe.db via ingress. Note: latency_ms in meta is
    # captured BEFORE the callback (we can't update once sent); the total
    # wall time including the callback shows up only in the debug log here.
    try:
        ingress_callback(ingress_url, f"/internal/clips/{clip_id}/transcribed", {
            "transcript": transcript_text,
            "segments": segments,
            "meta": baggage(
                WORKER, VERSION, latency_ms=sw.total_ms(), model=model,
                timings=sw.phases,
                extra={
                    "stt_model": model,
                    "audio_format": audio_format,
                    "stub": stub_mode or model == "stub",
                },
            ),
            "session_id": session_id,
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"transcribed clip {clip_id} ({len(transcript_text)} chars)",
        events=[{
            "type": "scribe.clip.transcribed.v1",
            "payload": {
                "session_id": session_id,
                "clip_id": clip_id,
                "transcript": transcript_text,
                "segments": segments,
            },
        }],
        logs=[
            {"level": "info", "message": f"clip {clip_id} transcribed (model={model})"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
