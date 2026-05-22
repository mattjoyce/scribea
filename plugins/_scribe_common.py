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
            prompt_version: str | None = None,
            timings: dict | None = None) -> dict[str, Any]:
    """Compose the scribe baggage envelope (specseed §4.4).

    `latency_ms` is the rolled-up total time inside the plugin's main work.
    `timings` is the disaggregated story: a named map of sub-phase milliseconds
    (`{ffmpeg_canonicalize_ms: 132, whisper_http_ms: 2200, …}`) used for
    bottleneck analysis. Both stay — the total is canonical, the breakdown
    explains it.
    """
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
    if timings is not None:
        out["timings"] = timings
    if extra:
        out.update(extra)
    return out


def with_timer():
    """Context-manager-like helper. Returns a callable that, when called, gives
    elapsed milliseconds since this function was first called. Cheaper than
    importing contextlib for one-liner timing."""
    start = time.perf_counter()
    return lambda: (time.perf_counter() - start) * 1000.0


class Stopwatch:
    """Sub-phase timer. Call ``mark(name)`` after each named phase; the elapsed
    ms since the last mark (or since construction) is recorded under ``name``
    in ``.phases``. Total wall time available via ``.total_ms()``. Cheap —
    uses ``time.perf_counter()``."""

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.last = self.start
        self.phases: dict[str, int] = {}

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.phases[name] = int((now - self.last) * 1000)
        self.last = now

    def total_ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)
