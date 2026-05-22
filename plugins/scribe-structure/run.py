#!/usr/bin/env python3
"""scribe-structure: turn the assembled context into a schema-valid SOAP JSON.

Uses Claude API when ANTHROPIC_API_KEY is wired; otherwise stubs a schema-
valid placeholder marked `meta.model=stub`. Re-running on the same prompt
hits the llm_cache for determinism in replay tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import (  # noqa: E402
    Stopwatch, baggage, err, ingress_callback, ok, read_request, write_response,
)

WORKER = "scribe-structure"
VERSION = "0.1.0"


def load_template(templates_dir: str, template_id: str) -> tuple[str, dict, dict]:
    """Return (system_prompt, schema, template_meta)."""
    base = os.path.join(templates_dir, template_id)
    with open(os.path.join(base, "system_prompt.md"), encoding="utf-8") as f:
        system_prompt = f.read()
    with open(os.path.join(base, "schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    with open(os.path.join(base, "template.json"), encoding="utf-8") as f:
        template_meta = json.load(f)
    return system_prompt, schema, template_meta


def render_few_shot(templates_dir: str, template_id: str) -> str:
    """Concatenate few-shot examples in lexical order as input/output blocks."""
    fs_dir = os.path.join(templates_dir, template_id, "few_shot")
    if not os.path.isdir(fs_dir):
        return ""
    out: list[str] = []
    for name in sorted(os.listdir(fs_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(fs_dir, name), encoding="utf-8") as f:
            ex = json.load(f)
        if "input" not in ex or "output" not in ex:
            continue
        out.append("# Example input\n" + ex["input"])
        out.append("# Example output\n" + json.dumps(ex["output"], indent=2))
    return "\n\n".join(out)


def validate_against_schema(obj: dict, schema: dict) -> str | None:
    """Tiny JSON-schema validator covering only the shapes we use (object,
    string, array, required, additionalProperties). Returns an error message
    or None.

    Avoids a jsonschema dependency for v0.
    """
    if schema.get("type") == "object":
        if not isinstance(obj, dict):
            return f"expected object, got {type(obj).__name__}"
        for req_key in schema.get("required", []) or []:
            if req_key not in obj:
                return f"missing required: {req_key}"
        if schema.get("additionalProperties") is False:
            allowed = set((schema.get("properties") or {}).keys())
            extra = set(obj.keys()) - allowed
            if extra:
                return f"unexpected fields: {sorted(extra)}"
        for key, subschema in (schema.get("properties") or {}).items():
            if key in obj:
                sub_err = validate_against_schema(obj[key], subschema)
                if sub_err:
                    return f"{key}: {sub_err}"
        return None
    if schema.get("type") == "string":
        if not isinstance(obj, str):
            return f"expected string, got {type(obj).__name__}"
        if "minLength" in schema and len(obj) < schema["minLength"]:
            return f"string too short (min {schema['minLength']})"
        return None
    if schema.get("type") == "array":
        if not isinstance(obj, list):
            return f"expected array, got {type(obj).__name__}"
        if "minItems" in schema and len(obj) < schema["minItems"]:
            return f"array too short (min {schema['minItems']})"
        items_schema = schema.get("items")
        if items_schema is not None:
            for i, item in enumerate(obj):
                sub_err = validate_against_schema(item, items_schema)
                if sub_err:
                    return f"[{i}]: {sub_err}"
        return None
    return None


def stub_structured(template_id: str) -> dict:
    """A schema-valid placeholder for the soap_consult template. Honest about
    its origin via the surrounding baggage."""
    return {
        "subjective": "(stub) No LLM key configured — assembled transcript was not analysed.",
        "objective": "(stub) No objective findings extracted.",
        "assessment": "(stub) Awaiting real LLM wiring; see ADR-0001.",
        "plan": ["Configure ANTHROPIC_API_KEY in scribe-structure plugin config."],
    }


def call_claude(api_key: str, model: str, system_prompt: str,
                user_content: str, timeout: float) -> dict:
    """Single non-cached call to Claude Messages API. Returns parsed JSON
    extracted from the response text.
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json_block(text: str) -> dict | None:
    """Extract the first balanced JSON object from a string. Handles both
    raw JSON and JSON inside ```json fences."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    # Scan for first { ... } balanced run.
    depth = 0
    start = -1
    for i, ch in enumerate(candidate):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blob = candidate[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    start = -1
    return None


def main() -> None:
    req = read_request()
    cmd = req.get("command")
    if cmd == "health":
        write_response(ok("scribe-structure alive"))
        return
    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    cfg = req.get("config") or {}
    ingress_url = cfg.get("ingress_url")
    templates_dir = cfg.get("templates_dir") or "./templates"
    anthropic_key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""
    model = cfg.get("claude_model") or "claude-sonnet-4-6"
    timeout = float(cfg.get("request_timeout_seconds", 90))
    stub_mode = str(cfg.get("stub_mode", "false")).lower() == "true"

    payload = (req.get("event") or {}).get("payload") or {}
    session_id = payload.get("session_id")
    template_id = payload.get("template_id") or "soap_consult"
    assembled_context = payload.get("assembled_context") or ""

    if not session_id:
        write_response(err("session_id missing", retry=False))
        return
    if not assembled_context:
        write_response(err("assembled_context missing", retry=False))
        return

    sw = Stopwatch()
    try:
        system_prompt, schema, template_meta = load_template(templates_dir, template_id)
    except Exception as e:  # noqa: BLE001
        write_response(err(f"template load failed: {e}", retry=False))
        return
    sw.mark("template_load_ms")

    few_shot = render_few_shot(templates_dir, template_id)
    user_content_parts = []
    if few_shot:
        user_content_parts.append(few_shot)
    user_content_parts.append("# Now produce structured JSON for the following transcript")
    user_content_parts.append(assembled_context)
    user_content_parts.append(
        "Output exactly one JSON object matching the schema. No prose."
    )
    user_content = "\n\n".join(user_content_parts)

    prompt_hash = hashlib.sha256(
        (system_prompt + "\n###\n" + user_content + "\n###\n" + model).encode("utf-8")
    ).hexdigest()
    sw.mark("prompt_build_ms")

    structured: dict | None = None
    raw_response: dict = {}
    used_model = "stub"
    tokens_in = 0
    tokens_out = 0
    notes: list[str] = []

    if stub_mode or not anthropic_key:
        structured = stub_structured(template_id)
        notes.append("LLM not configured — using stub structured output")
        sw.mark("stub_ms")
    else:
        try:
            raw_response = call_claude(anthropic_key, model, system_prompt, user_content, timeout)
            sw.mark("claude_http_ms")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            write_response(err(f"claude HTTP {e.code}: {body}", retry=e.code >= 500))
            return
        except urllib.error.URLError as e:
            write_response(err(f"claude network: {e}", retry=True))
            return
        except Exception as e:  # noqa: BLE001
            write_response(err(f"claude error: {e}", retry=True))
            return

        used_model = raw_response.get("model") or model
        usage = raw_response.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        content_blocks = raw_response.get("content") or []
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        structured = extract_json_block(text)
        if structured is None:
            # One retry with a stronger instruction.
            retry_content = user_content + "\n\nYour previous response did not parse as JSON. Output ONLY the JSON object now, no prose, no fences."
            try:
                raw_response = call_claude(anthropic_key, model, system_prompt, retry_content, timeout)
                text = "".join(b.get("text", "") for b in (raw_response.get("content") or []) if b.get("type") == "text")
                structured = extract_json_block(text)
                notes.append("recovered structured output on retry")
            except Exception:  # noqa: BLE001
                pass
        if structured is None:
            write_response(err("could not parse JSON from LLM response", retry=False))
            return

    schema_err = validate_against_schema(structured, schema)
    if schema_err:
        write_response(err(f"schema validation failed: {schema_err}", retry=False))
        return
    sw.mark("schema_validate_ms")

    try:
        ingress_callback(ingress_url, f"/internal/sessions/{session_id}/structured", {
            "structured": structured,
            "raw_llm_response": raw_response,
            "meta": baggage(
                WORKER, VERSION,
                latency_ms=sw.total_ms(),
                model=used_model,
                prompt_version=template_meta.get("prompt_version"),
                timings=sw.phases,
                extra={
                    "prompt_id": template_meta.get("prompt_id"),
                    "prompt_hash": prompt_hash,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "stub": stub_mode or used_model == "stub",
                    "notes": notes,
                },
            ),
        })
    except Exception as e:  # noqa: BLE001
        write_response(err(f"ingress callback failed: {e}", retry=True))
        return
    sw.mark("ingress_callback_ms")

    write_response(ok(
        f"structured {session_id} ({len(json.dumps(structured))} chars JSON)",
        events=[{
            "type": "scribe.session.structured.v1",
            "payload": {
                "session_id": session_id,
                "template_id": template_id,
                "structured": structured,
                "model": used_model,
                "prompt_version": template_meta.get("prompt_version"),
            },
        }],
        logs=[
            {"level": "info", "message": f"structured (model={used_model})"},
            {"level": "debug", "message": f"timings={sw.phases} total={sw.total_ms()}ms"},
        ],
    ))


if __name__ == "__main__":
    main()
