#!/usr/bin/env python3
"""scribe-clinical-ner: extract clinical and PHI entities from a clip transcript.

v0 ships as NOP — emits the full envelope with an empty entities list. The
slice exists so a real extractor (scispacy, GLiNER, regex) can drop in later
without touching contracts. See docs/scribe-ner-redact.md §1 for the full spec.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ok, read_request, write_response,
)

WORKER = "scribe-clinical-ner"
VERSION = "0.1.0"


def main() -> None:
    req = read_request()
    cmd = req.get("command")
    if cmd == "health":
        write_response(ok("scribe-clinical-ner alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")
    mode = (cfg.get("ner_mode") or os.environ.get("NER_MODE") or "nop").lower()

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    clip_id = payload.get("clip_id")
    transcript = payload.get("transcript") or ""

    if not (session_id and clip_id):
        write_response(err("missing session_id/clip_id in payload", retry=False))
        return

    sw = Stopwatch()

    # v0: only NOP is implemented. Real extractors swap inside this block.
    if mode != "nop":
        # Future modes (scispacy, gliner, regex) would dispatch here.
        write_response(err(
            f"ner_mode={mode!r} not implemented in v0; only 'nop' is supported",
            retry=False,
        ))
        return

    entities: list[dict] = []
    extractor = {
        "name": WORKER,
        "model": "nop",
        "version": VERSION,
        "ontology": None,
    }
    stats = {
        "transcript_chars": len(transcript),
        "entities_found": 0,
        "by_type": {},
    }
    sw.mark("nop_ms")

    try:
        ingress_callback(ingress_url, f"/internal/clips/{clip_id}/entities", {
            "session_id": session_id,
            "entities": entities,
            "extractor": extractor,
            "stats": stats,
            "meta": baggage(
                WORKER, VERSION, latency_ms=sw.total_ms(),
                model="nop", timings=sw.phases,
                extra={"mode": mode},
            ),
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"ner nop clip {clip_id} ({stats['transcript_chars']} chars)",
        events=[{
            "type": "scribe.clip.entities.v1",
            "payload": {
                "session_id": session_id,
                "clip_id": clip_id,
                "entities": entities,
                "extractor": extractor,
                "stats": stats,
            },
        }],
        logs=[
            {"level": "info", "message": f"ner-nop clip {clip_id}"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
