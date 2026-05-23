// Thin fetch wrappers around the ingress HTTP API. Same-origin, so no CORS dance.

const BASE = ""; // same origin (ingress serves PWA from /)

async function jsonFetch(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { "Accept": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* ignore */ }
    const err = new Error(`HTTP ${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res.text();
}

export const api = {
  healthz: () => jsonFetch("/healthz"),

  templates: () => jsonFetch("/templates"),

  listSessions: (limit = 50) => jsonFetch(`/sessions?limit=${encodeURIComponent(limit)}`),

  createSession: (template_id, meta = {}) =>
    jsonFetch("/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id, meta }),
    }),

  getSession: (id) => jsonFetch(`/sessions/${encodeURIComponent(id)}`),

  listClips: (id) => jsonFetch(`/sessions/${encodeURIComponent(id)}/clips`),

  // Multipart clip upload. Idempotency-Key is REQUIRED.
  uploadClip: async ({ session_id, clip_id, started_at, duration_ms, seq, audio_format, audio, blob }) => {
    const fd = new FormData();
    fd.append("clip_id", clip_id);
    fd.append("started_at", started_at);
    fd.append("duration_ms", String(duration_ms));
    fd.append("seq", String(seq));
    fd.append("audio_format", audio_format);
    // Named audio values captured by the recorder (mime, sample_rate_hz,
    // channels, bit_rate_bps, …). Ingress merges with ffprobe ground truth.
    if (audio) fd.append("audio_meta", JSON.stringify(audio));
    const filename = `clip-${clip_id}.${audio_format.includes("webm") ? "webm" : "m4a"}`;
    fd.append("audio", blob, filename);
    const res = await fetch(`${BASE}/sessions/${encodeURIComponent(session_id)}/clips`, {
      method: "POST",
      headers: { "Idempotency-Key": clip_id },
      body: fd,
    });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch { /* ignore */ }
      const err = new Error(`HTTP ${res.status} ${res.statusText}`);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return res.json();
  },

  closeSession: (id, close_reason = "user") =>
    jsonFetch(`/sessions/${encodeURIComponent(id)}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ close_reason }),
    }),

  getNote: (id) => jsonFetch(`/sessions/${encodeURIComponent(id)}/note`),

  getBaggage: (id) => jsonFetch(`/sessions/${encodeURIComponent(id)}/baggage`),

  clipAudioUrl: (sessionId, clipId) =>
    `${BASE}/sessions/${encodeURIComponent(sessionId)}/clips/${encodeURIComponent(clipId)}/audio`,
};
