#!/usr/bin/env python3
"""scribe-format: render the structured JSON to markdown via render.tmpl.

Avoids a jinja2 dependency for v0 by using a tiny templating subset that
matches what render.tmpl actually uses: {{ name }}, {% if name %} ... {% endif %},
{% for x in list %} ... {% endfor %}, and {{ loop.index }} inside loops.

This is deliberately under-powered — render.tmpl is small and lives in this
repo. If a future template needs richer logic, swap to real jinja2.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ok, read_request, write_response,
)

WORKER = "scribe-format"
VERSION = "0.1.0"


# {# ... #} comment blocks
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_IF = re.compile(r"\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}", re.DOTALL)
_FOR = re.compile(
    r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}",
    re.DOTALL,
)
_VAR = re.compile(r"\{\{\s*([\w\.]+)\s*\}\}")


def _get(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, "")
        else:
            cur = getattr(cur, part, "")
        if cur is None:
            return ""
    return cur


def _truthy(v: Any) -> bool:
    if v is None or v == "" or v == 0 or v is False:
        return False
    if isinstance(v, (list, dict, tuple, set)) and len(v) == 0:
        return False
    return True


def render(tmpl: str, ctx: dict[str, Any]) -> str:
    text = _COMMENT.sub("", tmpl)

    # Resolve `for` blocks first so their bodies can contain `if` and var refs.
    def for_repl(match: re.Match[str]) -> str:
        var_name, iter_name, body = match.group(1), match.group(2), match.group(3)
        items = ctx.get(iter_name) or []
        out: list[str] = []
        for i, item in enumerate(items):
            local_ctx = dict(ctx)
            local_ctx[var_name] = item
            local_ctx["loop"] = {"index": i + 1, "index0": i}
            out.append(render(body, local_ctx))
        return "".join(out)

    while True:
        new_text = _FOR.sub(for_repl, text)
        if new_text == text:
            break
        text = new_text

    def if_repl(match: re.Match[str]) -> str:
        name, body = match.group(1), match.group(2)
        return body if _truthy(ctx.get(name)) else ""

    while True:
        new_text = _IF.sub(if_repl, text)
        if new_text == text:
            break
        text = new_text

    def var_repl(match: re.Match[str]) -> str:
        return str(_get(ctx, match.group(1)))

    return _VAR.sub(var_repl, text)


def humanize_ms(ms: int) -> str:
    total_s = int(ms // 1000)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def main() -> None:
    req = read_request()
    cmd = req.get("command")
    if cmd == "health":
        write_response(ok("scribe-format alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")
    templates_dir = cfg.get("templates_dir") or "./templates"

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    template_id = payload.get("template_id") or "soap_consult"
    structured = payload.get("structured") or {}

    if not session_id:
        write_response(err("session_id missing", retry=False))
        return
    if not isinstance(structured, dict) or not structured:
        write_response(err("structured payload missing or empty", retry=False))
        return

    sw = Stopwatch()
    template_dir = os.path.join(templates_dir, template_id)
    try:
        with open(os.path.join(template_dir, "render.tmpl"), encoding="utf-8") as f:
            tmpl = f.read()
        with open(os.path.join(template_dir, "template.json"), encoding="utf-8") as f:
            template_meta = json.load(f)
    except Exception as e:  # noqa: BLE001
        write_response(err(f"template load failed: {e}", retry=False))
        return
    sw.mark("template_load_ms")

    # Fetch session view for header fields.
    try:
        from _scribe_common import ingress_get  # noqa: WPS433
        session_view = ingress_get(ingress_url, f"/sessions/{session_id}")
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress GET failed: {e}", retry=True))
        return
    sw.mark("ingress_get_ms")

    session = session_view.get("session") or {}
    clips = session_view.get("clips") or []
    total_ms = sum(int((c.get("duration_ms") or 0)) for c in clips)
    gaps_count = sum(1 for c in clips if c.get("state") == "failed")

    ctx: dict[str, Any] = {
        "session_id": session_id,
        "template_version": template_meta.get("version", "?"),
        "started_at": session.get("started_at", ""),
        "duration_human": humanize_ms(total_ms),
        "clip_count": len(clips),
        "gaps": gaps_count,
        "model": payload.get("model") or "",
        "prompt_version": payload.get("prompt_version") or template_meta.get("prompt_version", ""),
        # Expose the full structured dict so any template can reference its
        # own fields via `{{ structured.<field> }}`. The four SOAP-specific
        # top-level keys below are kept for backwards compatibility with
        # the soap_consult/render.tmpl shape.
        "structured": structured,
        "subjective": structured.get("subjective", ""),
        "objective": structured.get("objective", ""),
        "assessment": structured.get("assessment", ""),
        "plan": structured.get("plan", []),
    }
    markdown = render(tmpl, ctx)
    sw.mark("render_ms")

    try:
        ingress_callback(ingress_url, f"/internal/sessions/{session_id}/completed", {
            "markdown": markdown,
            "structured": structured,
            "meta": baggage(
                WORKER, VERSION, latency_ms=sw.total_ms(), timings=sw.phases,
                extra={"template_id": template_id, "char_count": len(markdown)},
            ),
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"formatted {session_id} ({len(markdown)} chars)",
        events=[{
            "type": "scribe.session.completed.v1",
            "payload": {"session_id": session_id, "markdown": markdown},
        }],
        logs=[
            {"level": "info", "message": "session completed"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
