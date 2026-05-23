#!/usr/bin/env python3
"""scribe-assemble: read all clips for a session, compose the assembled context.

Pure function of (clips in seq order, template_id). Re-running on the same
input must produce byte-identical assembled_context — that's the cache key
for the structure-worker's llm_cache.

Per docs/scribe-ner-redact.md §3.1, the default transcript source is the
*redacted* blob (reached via clips.redacted_transcript_ref). The env var
ASSEMBLE_TRANSCRIPT_SOURCE flips between 'redacted' (default) and 'original'
(read clips.transcript directly). The chosen source is stamped into the
emitted assembled event so the audit trail records which version the LLM saw.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ingress_get, ok, read_request,
    write_response,
)

WORKER = "scribe-assemble"
VERSION = "0.3.0"


def _clip_ready(c: dict) -> bool:
    """A clip is ready for assembly when its per-clip pipeline has reached a
    terminal state: 'failed' (becomes a gap), or 'transcribed' with the
    redacted_transcript_ref stamped (so we can read the canonical blob)."""
    state = c.get("state")
    if state == "failed":
        return True
    if state == "transcribed" and c.get("redacted_transcript_ref"):
        return True
    return False


def wait_for_clips(ingress_url: str, session_id: str, *,
                   max_wait_s: float = 30.0,
                   poll_interval_s: float = 0.25) -> tuple[dict, int, bool]:
    """Poll /sessions/{id} until every clip is terminal-ready, or budget hits.

    The race we're closing: assemble fires on scribe.session.close_requested.v1
    immediately, but the per-clip pipeline (preprocess → transcribe → ner →
    redact) for already-uploaded clips is still in flight. Without waiting,
    assemble would write gap markers for every clip and structure would see a
    useless context.

    The wait is bounded so a stuck transcribe doesn't pin a Ductile worker
    forever — past the deadline we assemble with whatever's done, marking
    the rest as gaps (the same path that already handles permanent failures).

    Returns (final_session_view, elapsed_ms, timed_out).
    """
    start = time.perf_counter()
    deadline = start + max_wait_s
    view = ingress_get(ingress_url, f"/sessions/{session_id}")
    while True:
        clips = view.get("clips") or []
        # Empty session: nothing to wait for; let assemble note 0 clips.
        if not clips or all(_clip_ready(c) for c in clips):
            return view, int((time.perf_counter() - start) * 1000), False
        if time.perf_counter() >= deadline:
            return view, int((time.perf_counter() - start) * 1000), True
        time.sleep(poll_interval_s)
        view = ingress_get(ingress_url, f"/sessions/{session_id}")


def _format_mmss(ms: int) -> str:
    total_s = max(0, int(ms // 1000))
    return f"{total_s // 60:02d}:{total_s % 60:02d}"


def _read_blob(blobs_dir: str, ref: str) -> str | None:
    """Read a content-addressed text blob. ref is 'sha256:<hex>' or bare hex.
    Returns the decoded text, or None if the blob is missing."""
    if not ref:
        return None
    sha = ref.split(":", 1)[1] if ":" in ref else ref
    path = os.path.join(blobs_dir, sha)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def _clip_transcript(c: dict, source: str, blobs_dir: str) -> str | None:
    """Return the transcript text for a clip, honouring the configured source.
    Falls back to clips.transcript if 'redacted' is requested but no blob
    exists yet (e.g. NER/redact failed mid-pipeline)."""
    if source == "original":
        return c.get("transcript")
    ref = c.get("redacted_transcript_ref")
    if ref and blobs_dir:
        text = _read_blob(blobs_dir, ref)
        if text is not None:
            return text
    # Fallback: redact didn't run or blob missing. Use original so the pipeline
    # still produces a note; the source field in the emitted event records this.
    return c.get("transcript")


def compose(session: dict, clips: list[dict], template_id: str, *,
            source: str, blobs_dir: str) -> tuple[str, list[dict], str]:
    """Return (assembled_context, gaps[], effective_source).

    effective_source is what was actually used per clip — 'redacted' if every
    transcribed clip's redacted blob was read, 'original' if any fell back
    (or if the env explicitly requested original)."""
    total_ms = sum(int(c.get("duration_ms") or 0) for c in clips)
    gaps: list[dict] = []
    lines: list[str] = []
    lines.append(f"# Session {session['session_id']}")
    lines.append(f"template: {template_id}")
    lines.append(f"clips: {len(clips)}")
    lines.append(f"total_duration: {_format_mmss(total_ms)}")
    lines.append("")

    fell_back = False
    for c in clips:
        seq = c.get("seq", 0)
        offset = _format_mmss(int(c.get("duration_ms") or 0))  # per-clip relative; absolute would need running sum
        marker = f"[clip {seq}, {offset}]"
        transcript_text = _clip_transcript(c, source, blobs_dir)
        if source == "redacted" and not c.get("redacted_transcript_ref") and c.get("transcript"):
            fell_back = True
        if c.get("state") == "transcribed" and transcript_text:
            lines.append(marker)
            lines.append((transcript_text or "").strip())
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

    effective = "redacted" if (source == "redacted" and not fell_back) else (
        "original" if source == "original" else "redacted_with_fallback"
    )
    return "\n".join(lines).strip() + "\n", gaps, effective


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
    blobs_dir = cfg.get("blobs_dir") or os.environ.get("BLOBS_DIR") or ""
    # Spec §3.1: default to redacted; env var (or plugin config) flips it.
    source = (cfg.get("transcript_source")
              or os.environ.get("ASSEMBLE_TRANSCRIPT_SOURCE")
              or "redacted").lower()
    if source not in ("redacted", "original"):
        source = "redacted"
    # Per-clip-readiness wait budget — tunable so tests/replays can shorten it.
    max_wait_s = float(cfg.get("max_wait_for_clips_seconds")
                       or os.environ.get("ASSEMBLE_MAX_WAIT_SECONDS")
                       or 30.0)
    poll_interval_s = float(cfg.get("poll_interval_seconds")
                            or os.environ.get("ASSEMBLE_POLL_INTERVAL_SECONDS")
                            or 0.25)

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    template_id = payload.get("template_id")
    if not session_id:
        write_response(err("session_id missing", retry=False))
        return

    sw = Stopwatch()
    try:
        session_view, wait_ms, wait_timed_out = wait_for_clips(
            ingress_url, session_id,
            max_wait_s=max_wait_s, poll_interval_s=poll_interval_s,
        )
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress GET failed: {e}", retry=True))
        return
    sw.phases["wait_for_clips_ms"] = wait_ms
    sw.last = time.perf_counter()

    session = session_view.get("session") or {}
    clips = sorted(session_view.get("clips") or [], key=lambda c: c.get("seq", 0))
    if not template_id:
        template_id = session.get("template_id")

    assembled_context, gaps, effective_source = compose(
        session, clips, template_id or "unknown",
        source=source, blobs_dir=blobs_dir,
    )
    sw.mark("compose_ms")
    try:
        ingress_callback(ingress_url, f"/internal/sessions/{session_id}/assembled", {
            "assembled_context": assembled_context,
            "gaps": gaps,
            "transcript_source": effective_source,
            "meta": baggage(
                WORKER, VERSION, latency_ms=sw.total_ms(), timings=sw.phases,
                extra={"clip_count": len(clips), "gap_count": len(gaps),
                       "template_id": template_id,
                       "transcript_source": effective_source,
                       "transcript_source_requested": source,
                       "wait_timed_out": wait_timed_out},
            ),
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"assembled {len(clips)} clips ({len(gaps)} gaps, source={effective_source})",
        events=[{
            "type": "scribe.session.assembled.v1",
            "payload": {
                "session_id": session_id,
                "template_id": template_id,
                "assembled_context": assembled_context,
                "gaps": gaps,
                "transcript_source": effective_source,
            },
        }],
        logs=[
            {"level": "info", "message": f"assembled {session_id} source={effective_source}"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
