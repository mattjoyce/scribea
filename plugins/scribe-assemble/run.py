#!/usr/bin/env python3
"""scribe-assemble: read all clips for a session, compose the assembled context.

Pure function of (clips in seq order, template_id, latest EMR Backstory).
Re-running on the same input must produce byte-identical assembled_context —
that's the cache key for the structure-worker's llm_cache.

The assembled context has two sections when EMR backstory is attached:

    EMR Backstory
    =============
    Demographics: <fields>
    Admission reason: <text>

    Previous notes:
    <text>

    Dictation transcript
    ====================
    [clip 1, mm:ss]
    ...

When no scribe.case.context_attached.v1 event exists for the session, the
EMR Backstory section is omitted and only the transcript section is produced
(preserving the v0 session-without-context flow unchanged).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    baggage, err, ingress_callback, ingress_get, ok, read_request,
    with_timer, write_response,
)

WORKER = "scribe-assemble"
VERSION = "0.1.0"


def _format_mmss(ms: int) -> str:
    total_s = max(0, int(ms // 1000))
    return f"{total_s // 60:02d}:{total_s % 60:02d}"


def _format_backstory(ctx_data: dict) -> list[str]:
    """Render an EMR Backstory block from a scribe.case.context_attached.v1
    event's data payload. Returns lines to be prepended to the assembled
    context. Empty list if no useful content."""
    demo = ctx_data.get("demographics") or {}
    previous_notes = (ctx_data.get("previous_notes") or "").strip()
    if not demo and not previous_notes:
        return []

    lines: list[str] = []
    lines.append("EMR Backstory")
    lines.append("=============")
    bits: list[str] = []
    if demo.get("display_name"):
        bits.append(str(demo["display_name"]))
    age = demo.get("age")
    sex = demo.get("sex")
    if age is not None and sex:
        bits.append(f"{age}{sex}")
    elif age is not None:
        bits.append(str(age))
    elif sex:
        bits.append(str(sex))
    if demo.get("icu_day") is not None:
        bits.append(f"ICU day {demo['icu_day']}")
    if bits:
        lines.append("Demographics: " + ", ".join(bits))
    if demo.get("admission_reason"):
        lines.append(f"Admission reason: {demo['admission_reason']}")
    if previous_notes:
        lines.append("")
        lines.append("Previous notes:")
        lines.append(previous_notes)
    lines.append("")
    return lines


def compose(session: dict, clips: list[dict], template_id: str,
            backstory_lines: list[str] | None = None) -> tuple[str, list[dict]]:
    """Return (assembled_context, gaps[])."""
    total_ms = sum(int(c.get("duration_ms") or 0) for c in clips)
    gaps: list[dict] = []
    lines: list[str] = []
    if backstory_lines:
        lines.extend(backstory_lines)
        lines.append("Dictation transcript")
        lines.append("====================")
    lines.append(f"# Session {session['session_id']}")
    lines.append(f"template: {template_id}")
    lines.append(f"clips: {len(clips)}")
    lines.append(f"total_duration: {_format_mmss(total_ms)}")
    lines.append("")

    for c in clips:
        seq = c.get("seq", 0)
        offset = _format_mmss(int(c.get("duration_ms") or 0))  # per-clip relative; absolute would need running sum
        marker = f"[clip {seq}, {offset}]"
        if c.get("state") == "transcribed" and c.get("transcript"):
            lines.append(marker)
            lines.append((c.get("transcript") or "").strip())
            lines.append("")
        elif c.get("state") == "failed":
            dur_s = int((c.get("duration_ms") or 0) // 1000)
            gap_line = f"{marker}: transcription failed — {dur_s} seconds of audio missing"
            lines.append("[" + gap_line + "]")
            lines.append("")
            gaps.append({"clip_id": c.get("clip_id"), "seq": seq, "duration_ms": c.get("duration_ms")})
        else:
            # Still uploading or transcribing — treat as a gap for assembly purposes
            # so a slow upstream doesn't block the whole pipeline.
            dur_s = int((c.get("duration_ms") or 0) // 1000)
            gap_line = f"{marker}: not yet transcribed — {dur_s} seconds of audio missing"
            lines.append("[" + gap_line + "]")
            lines.append("")
            gaps.append({
                "clip_id": c.get("clip_id"),
                "seq": seq,
                "duration_ms": c.get("duration_ms"),
                "incomplete": True,
            })

    return "\n".join(lines).strip() + "\n", gaps


def main() -> None:
    req = read_request()
    cmd = req.get("command")
    if cmd == "health":
        write_response(ok("scribe-assemble alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    template_id = payload.get("template_id")
    if not session_id:
        write_response(err("session_id missing", retry=False))
        return

    elapsed = with_timer()
    try:
        session_view = ingress_get(ingress_url, f"/sessions/{session_id}")
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress GET failed: {e}", retry=True))
        return

    session = session_view.get("session") or {}
    clips = sorted(session_view.get("clips") or [], key=lambda c: c.get("seq", 0))
    if not template_id:
        template_id = session.get("template_id")

    # Look for the latest EMR-context-attached event so the LLM sees the
    # established facts (ADR-0006). Latest wins if multiple were stamped.
    backstory_lines: list[str] = []
    try:
        events = ingress_get(ingress_url, f"/sessions/{session_id}/baggage")
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress GET baggage failed: {e}", retry=True))
        return
    if isinstance(events, list):
        ctx_events = [e for e in events
                      if e.get("event_type") == "scribe.case.context_attached.v1"]
        if ctx_events:
            ctx_events.sort(key=lambda e: e.get("event_time", ""))
            backstory_lines = _format_backstory(ctx_events[-1].get("data") or {})

    assembled_context, gaps = compose(
        session, clips, template_id or "unknown", backstory_lines=backstory_lines,
    )
    meta = baggage(
        WORKER, VERSION, latency_ms=elapsed(),
        extra={"clip_count": len(clips), "gap_count": len(gaps), "template_id": template_id},
    )
    try:
        ingress_callback(ingress_url, f"/internal/sessions/{session_id}/assembled", {
            "assembled_context": assembled_context,
            "gaps": gaps,
            "meta": meta,
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return

    write_response(ok(
        f"assembled {len(clips)} clips ({len(gaps)} gaps)",
        events=[{
            "type": "scribe.session.assembled.v1",
            "payload": {
                "session_id": session_id,
                "template_id": template_id,
                "assembled_context": assembled_context,
                "gaps": gaps,
            },
        }],
        logs=[{"level": "info", "message": f"assembled {session_id}"}],
    ))


if __name__ == "__main__":
    main()
