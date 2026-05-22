// EventSource subscription helper. Returns a handle with .close().
// Spec: server emits `event: <event_type>\ndata: <eventRow JSON>\n\n`.

const EVENT_TYPES = [
  "scribe.session.created.v1",
  "scribe.clip.received.v1",
  "scribe.clip.transcribed.v1",
  "scribe.clip.failed.v1",
  "scribe.session.close_requested.v1",
  "scribe.session.assembled.v1",
  "scribe.session.structured.v1",
  "scribe.session.completed.v1",
  "scribe.session.failed.v1",
];

export function subscribe(sessionId, handler) {
  const url = `/sessions/${encodeURIComponent(sessionId)}/live`;
  const es = new EventSource(url);
  const listeners = [];

  const wrap = (type) => (e) => {
    let row = null;
    try { row = JSON.parse(e.data); } catch { row = { raw: e.data }; }
    try { handler({ type, row }); } catch (err) {
      console.error("SSE handler error", type, err);
    }
  };

  for (const t of EVENT_TYPES) {
    const fn = wrap(t);
    es.addEventListener(t, fn);
    listeners.push([t, fn]);
  }

  // Also catch a default "message" event in case server omits the event: line.
  const onMsg = (e) => {
    let row = null;
    try { row = JSON.parse(e.data); } catch { row = { raw: e.data }; }
    handler({ type: row && row.event_type ? row.event_type : "message", row });
  };
  es.addEventListener("message", onMsg);

  es.onerror = (e) => {
    console.warn("SSE error", e);
    // EventSource will auto-reconnect; we just log.
  };

  return {
    close() {
      for (const [t, fn] of listeners) es.removeEventListener(t, fn);
      es.removeEventListener("message", onMsg);
      es.close();
    },
  };
}
