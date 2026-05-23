#!/usr/bin/env python3
"""scribe-redact: produce a redacted transcript by masking PHI entity spans.

Two modes (default is ``active``):

- ``active``: POST the transcript to the deployed PHI detector (default
  ``http://192.168.20.4:8890/redact`` per ``docs/phi-redact.md``), receive
  the redacted text + audit list, write the redacted text as a content-
  addressed blob, stamp the audit into ``clips.redactions``. The
  ``redacted_transcript_ref`` blob now differs from the original.
- ``passthrough``: original behaviour for tests and replays — write the
  original transcript as a blob unchanged, emit empty ``redactions[]``,
  refs equal. Kept so the v0 NOP path is still reachable.

The original transcript stays in ``clips.transcript`` for the POC (a
deliberate divergence from ``docs/phi-redact.md`` §16 — accepted in
``docs/scribe-ner-redact.md`` §0 for traceability; downstream stages still
consume the redacted ref by default).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ingress_get, ok, read_request,
    write_response,
)

WORKER = "scribe-redact"
VERSION = "0.2.0"

DEFAULT_PHI_URL = "http://192.168.20.4:8890/redact"
DEFAULT_PLACEHOLDER = "[{label}]"


def _write_blob(blobs_dir: str, content: bytes) -> str:
    """Content-address ``content`` and atomically place it under blobs_dir.
    Returns the bare hex digest (no ``sha256:`` prefix). Idempotent — if a
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


def _call_phi_detector(phi_url: str, text: str, placeholder_format: str,
                       timeout: float) -> dict:
    """POST the transcript to the PHI detector, return the parsed response.
    Per docs/phi-redact.md §50, the response is ``{model, text, entities}``
    with the audit ``entities`` carrying char-offsets into the *input* text.
    """
    body = json.dumps({
        "text": text,
        "placeholder_format": placeholder_format,
    }).encode("utf-8")
    req = urllib.request.Request(
        phi_url,
        data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _map_audit_to_redactions(audit: list[dict], placeholder_format: str) -> list[dict]:
    """Translate the phi-detector audit shape to our redactions[] schema
    (scribe-ner-redact.md §2.5). The detector's label is kept verbatim
    (snake_case strings like ``first_name``) per phi-redact.md §142 —
    scribea treats unknown labels gracefully rather than mapping into our
    NER vocabulary.

    Adds the detector's confidence as a ``score`` field (additive
    extension of our schema; harmless for consumers that ignore it).
    """
    out: list[dict] = []
    for e in audit or []:
        label = e.get("label") or "UNKNOWN"
        out.append({
            "entity_type": label,
            "original_text": e.get("text") or "",
            "replacement_text": placeholder_format.format(label=label),
            "start": int(e.get("start") or 0),
            "end": int(e.get("end") or 0),
            "score": e.get("score"),
            "source_entity_index": None,  # detector runs its own model; no NER cross-ref
        })
    return out


def _emit_redact_failed(session_id: str, clip_id: str, reason: str) -> None:
    write_response(ok(
        f"redact failed — {reason}",
        events=[{
            "type": "scribe.clip.redact_failed.v1",
            "payload": {
                "session_id": session_id,
                "clip_id": clip_id,
                "reason": reason,
            },
        }],
        logs=[{"level": "error", "message": f"redact failed: {reason}"}],
    ))


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
    mode = (cfg.get("redact_mode") or os.environ.get("REDACT_MODE") or "active").lower()
    phi_url = cfg.get("phi_url") or os.environ.get("PHI_URL") or DEFAULT_PHI_URL
    placeholder_format = (cfg.get("redact_placeholder_format")
                          or os.environ.get("REDACT_PLACEHOLDER_FORMAT")
                          or DEFAULT_PLACEHOLDER)
    phi_timeout = float(cfg.get("phi_request_timeout_seconds")
                        or os.environ.get("PHI_REQUEST_TIMEOUT_SECONDS") or 30.0)

    if not blobs_dir:
        write_response(err("blobs_dir not configured", retry=False))
        return

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    clip_id = payload.get("clip_id")
    # NER's entities may be missing if NER failed. Active redact doesn't need
    # them (the detector has its own model), but we propagate the warning.
    entities = payload.get("entities")
    ner_unavailable = entities is None

    if not (session_id and clip_id):
        write_response(err("missing session_id/clip_id in payload", retry=False))
        return

    sw = Stopwatch()

    # Fetch the clip to read the original transcript via ingress.
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
    if transcript is None or transcript == "":
        _emit_redact_failed(session_id, clip_id, "no_transcript")
        return

    # --- redact ---------------------------------------------------------
    warnings: list[str] = []
    if ner_unavailable:
        warnings.append("ner_unavailable")

    if mode == "passthrough":
        redacted_text = transcript
        redactions: list[dict] = []
        phi_model = "passthrough"
        passthrough = True
        mask_strategy = "none"
    elif mode == "active":
        try:
            resp = _call_phi_detector(phi_url, transcript, placeholder_format, phi_timeout)
            sw.mark("phi_http_ms")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            # 4xx is permanent (bad input); 5xx is retryable per phi-redact §132.
            retry = e.code >= 500
            write_response(err(f"phi-detector HTTP {e.code}: {body}", retry=retry))
            return
        except urllib.error.URLError as e:
            write_response(err(f"phi-detector unreachable: {e}", retry=True))
            return
        except Exception as e:  # noqa: BLE001
            write_response(err(f"phi-detector error: {e}", retry=True))
            return
        redacted_text = resp.get("text") or ""
        if not redacted_text:
            _emit_redact_failed(session_id, clip_id, "phi_detector_returned_empty_text")
            return
        redactions = _map_audit_to_redactions(resp.get("entities") or [], placeholder_format)
        phi_model = resp.get("model") or "unknown"
        passthrough = False
        mask_strategy = f"placeholder:{placeholder_format}"
    else:
        write_response(err(
            f"redact_mode={mode!r} not implemented; use 'active' or 'passthrough'",
            retry=False,
        ))
        return

    # --- persist the redacted blob -------------------------------------
    try:
        original_sha = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        redacted_sha = _write_blob(blobs_dir, redacted_text.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        _emit_redact_failed(session_id, clip_id, f"blob_write_failed: {e}")
        return
    sw.mark("blob_write_ms")

    original_ref = "sha256:" + original_sha
    redacted_ref = "sha256:" + redacted_sha

    redactor: dict = {
        "name": WORKER,
        "mode": mode,
        "version": VERSION,
        "mask_strategy": mask_strategy,
    }
    if warnings:
        redactor["warnings"] = warnings

    redacted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        ingress_callback(ingress_url, f"/internal/clips/{clip_id}/redacted", {
            "session_id": session_id,
            "redacted_transcript_ref": redacted_ref,
            "original_transcript_ref": original_ref,
            "redactions": redactions,
            "passthrough": passthrough,
            "redactor": redactor,
            "meta": baggage(
                WORKER, VERSION, latency_ms=sw.total_ms(),
                model=phi_model, timings=sw.phases,
                extra={
                    "mode": mode,
                    "ner_unavailable": ner_unavailable,
                    "transcript_chars": len(transcript),
                    "redactions_count": len(redactions),
                    "phi_model": phi_model,
                    "phi_redacted_at": redacted_at,
                    "phi_url": phi_url if mode == "active" else None,
                    "placeholder_format": placeholder_format,
                },
            ),
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"redact {mode} clip {clip_id} ({len(redactions)} spans) → {redacted_ref}",
        events=[{
            "type": "scribe.clip.redacted.v1",
            "payload": {
                "session_id": session_id,
                "clip_id": clip_id,
                "redacted_transcript_ref": redacted_ref,
                "original_transcript_ref": original_ref,
                "redactions": redactions,
                "passthrough": passthrough,
                "redactor": redactor,
            },
        }],
        logs=[
            {"level": "info",
             "message": f"redact-{mode} clip {clip_id} model={phi_model} spans={len(redactions)}"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
