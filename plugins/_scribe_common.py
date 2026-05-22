"""Shared helpers for scribea Ductile plugins.

Importable from each plugin's run.py because Ductile sets the working
directory to the plugin's dir, and we add the parent on sys.path at the top
of each plugin to find this module. Kept tiny on purpose — no heavy deps.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def read_request() -> dict[str, Any]:
    """Read the Ductile Protocol 2 request envelope from stdin."""
    raw = sys.stdin.read()
    return json.loads(raw)


def write_response(resp: dict[str, Any]) -> None:
    """Write the Ductile Protocol 2 response envelope to stdout (single JSON)."""
    json.dump(resp, sys.stdout)
    sys.stdout.flush()


def ok(result: str, *, events: list[dict] | None = None,
       logs: list[dict] | None = None, state_updates: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "ok", "result": result}
    if events:
        out["events"] = events
    if logs:
        out["logs"] = logs
    if state_updates:
        out["state_updates"] = state_updates
    return out


def err(message: str, *, retry: bool = True,
        logs: list[dict] | None = None) -> dict[str, Any]:
    return {
        "status": "error",
        "error": message,
        "retry": retry,
        "logs": logs or [{"level": "error", "message": message}],
    }


def ingress_callback(ingress_url: str, path: str, body: dict[str, Any],
                     timeout: float = 30.0) -> dict[str, Any]:
    """POST JSON to the scribe-ingress /internal/* endpoint. Returns parsed JSON."""
    if not ingress_url:
        raise RuntimeError("INGRESS_URL not configured")
    url = ingress_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ingress_get(ingress_url: str, path: str, timeout: float = 30.0) -> dict[str, Any]:
    if not ingress_url:
        raise RuntimeError("INGRESS_URL not configured")
    url = ingress_url.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def baggage(worker: str, worker_version: str, *, extra: dict | None = None,
            latency_ms: float | None = None, model: str | None = None,
            prompt_version: str | None = None) -> dict[str, Any]:
    """Compose the scribe baggage envelope (specseed §4.4)."""
    out: dict[str, Any] = {
        "worker": worker,
        "worker_version": worker_version,
        "node": os.uname().nodename,
    }
    if latency_ms is not None:
        out["latency_ms"] = int(latency_ms)
    if model is not None:
        out["model"] = model
    if prompt_version is not None:
        out["prompt_version"] = prompt_version
    if extra:
        out.update(extra)
    return out


def with_timer():
    """Context-manager-like helper. Returns a callable that, when called, gives
    elapsed milliseconds since this function was first called. Cheaper than
    importing contextlib for one-liner timing."""
    start = time.perf_counter()
    return lambda: (time.perf_counter() - start) * 1000.0
