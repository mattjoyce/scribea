#!/usr/bin/env python3
"""scribe-redact: produce a redacted transcript by masking PHI entity spans.

v0 ships in passthrough mode — the redacted blob is byte-identical to the
original, so `redacted_transcript_ref == original_transcript_ref` (CAS dedupes).
Downstream stages consume the redacted ref by default; when real redaction
lands, only this plugin's logic changes. See docs/scribe-ner-redact.md §2.

Materialisation choice (per spec §2.6): always write the original transcript
as a blob at sha256(transcript). Idempotent via CAS — same content, same path.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ingress_get, ok, read_request,
    write_response,
)

WORKER = "scribe-redact"
VERSION = "0.1.0"


def _write_blob(blobs_dir: str, content: bytes) -> str:
    """Content-address `content` and atomically place it under blobs_dir.
    Returns the bare hex digest (no `sha256:` prefix). Idempotent — if a
    blob with this hash already exists, no write happens.
    """
    sha = hashlib.sha256(content).hexdigest()
    dest = os.path.join(blobs_dir, sha)
    if os.path.exists(dest):
        return sha
    os.makedirs(blobs_dir, exist_ok=True)
    tmp = dest + ".inflight"
    try:
        with open(tmp, "wb") as f:
            f.write(content)
        os.rename(tmp, dest)
    except FileExistsError:
        # Concurrent writer beat us to the rename — harmless, same content.
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return sha


def main() -> None:
    req = read_request()
    cmd = req.get("command")
    if cmd == "health":
        write_response(ok("scribe-redact alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")
    blobs_dir = cfg.get("blobs_dir") or os.environ.get("BLOBS_DIR")
    mode = (cfg.get("redact_mode") or os.environ.get("REDACT_MODE") or "passthrough").lower()

    if not blobs_dir:
        write_response(err("blobs_dir not configured", retry=False))
        return

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    clip_id = payload.get("clip_id")
    # entities may be missing if NER failed — degraded passthrough per §2.9.
    entities = payload.get("entities")
    ner_unavailable = entities is None

    if not (session_id and clip_id):
        write_response(err("missing session_id/clip_id in payload", retry=False))
        return

    sw = Stopwatch()

    # Fetch the clip to read the original transcript. The PWA → ingress path
    # writes it; assemble already reads via ingress_get, so the contract is
    # established.
    try:
        clip_view = ingress_get(ingress_url, f"/sessions/{session_id}")
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress GET session failed: {e}", retry=True))
        return
    sw.mark("ingress_get_ms")

    transcript = None
    for c in (clip_view.get("clips") or []):
        if c.get("clip_id") == clip_id:
            transcript = c.get("transcript")
            break
    if transcript is None:
        # Not transcribed (or failed). Spec §2.9 says emit redact_failed.v1.
        write_response(ok(
            "redact skipped — no transcript",
            events=[{
                "type": "scribe.clip.redact_failed.v1",
                "payload": {
                    "session_id": session_id,
                    "clip_id": clip_id,
                    "reason": "no_transcript",
                },
            }],
            logs=[{"level": "warn", "message": f"clip {clip_id} has no transcript"}],
        ))
        return

    if mode != "passthrough":
        # Future: active redaction dispatch here.
        write_response(err(
            f"redact_mode={mode!r} not implemented in v0; only 'passthrough' is supported",
            retry=False,
        ))
        return

    # Materialise the original transcript as a blob. CAS dedupes on replay.
    try:
        text_bytes = transcript.encode("utf-8")
        sha = _write_blob(blobs_dir, text_bytes)
    except Exception as e:  # noqa: BLE001
        write_response(ok(
            "redact failed — blob_write_failed",
            events=[{
                "type": "scribe.clip.redact_failed.v1",
                "payload": {
                    "session_id": session_id,
                    "clip_id": clip_id,
                    "reason": f"blob_write_failed: {e}",
                },
            }],
            logs=[{"level": "error", "message": f"blob write failed: {e}"}],
        ))
        return
    sw.mark("blob_write_ms")

    original_ref = "sha256:" + sha
    redacted_ref = original_ref  # passthrough: identical content → identical ref

    redactor: dict = {
        "name": WORKER,
        "mode": "passthrough",
        "version": VERSION,
        "mask_strategy": "none",
    }
    if ner_unavailable:
        redactor["warnings"] = ["ner_unavailable"]

    try:
        ingress_callback(ingress_url, f"/internal/clips/{clip_id}/redacted", {
            "session_id": session_id,
            "redacted_transcript_ref": redacted_ref,
            "original_transcript_ref": original_ref,
            "redactions": [],
            "passthrough": True,
            "redactor": redactor,
            "meta": baggage(
                WORKER, VERSION, latency_ms=sw.total_ms(),
                model="passthrough", timings=sw.phases,
                extra={
                    "mode": mode,
                    "ner_unavailable": ner_unavailable,
                    "transcript_chars": len(transcript),
                },
            ),
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"redact passthrough clip {clip_id} → {original_ref}",
        events=[{
            "type": "scribe.clip.redacted.v1",
            "payload": {
                "session_id": session_id,
                "clip_id": clip_id,
                "redacted_transcript_ref": redacted_ref,
                "original_transcript_ref": original_ref,
                "redactions": [],
                "passthrough": True,
                "redactor": redactor,
            },
        }],
        logs=[
            {"level": "info", "message": f"redact-passthrough clip {clip_id} ref={original_ref}"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
