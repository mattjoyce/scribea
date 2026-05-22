#!/usr/bin/env python3
"""scribe-event-relay: read {event_type, data} from ingress, emit it as a Ductile event.

The plugin exists solely so scribe-ingress can submit events into Ductile
via a single well-known plugin call rather than learning the bus protocol.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _scribe_common import err, ok, read_request, write_response  # noqa: E402


def main() -> None:
    req = read_request()
    cmd = req.get("command")

    if cmd == "health":
        write_response(ok("scribe-event-relay alive"))
        return

    if cmd != "handle":
        write_response(err(f"unknown command: {cmd}", retry=False))
        return

    payload = (req.get("event") or {}).get("payload") or {}
    event_type = payload.get("event_type")
    data = payload.get("data") or {}

    if not event_type or not isinstance(event_type, str):
        write_response(err("payload.event_type missing or not string", retry=False))
        return
    if not event_type.startswith("scribe."):
        # Defensive — we only relay scribe events.
        write_response(err(f"refusing to relay non-scribe event: {event_type}", retry=False))
        return

    write_response(ok(
        f"relayed {event_type}",
        events=[{"type": event_type, "payload": data}],
        logs=[{"level": "info", "message": f"relayed {event_type}"}],
    ))


if __name__ == "__main__":
    main()
