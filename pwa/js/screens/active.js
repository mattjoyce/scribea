// Active session — start/stop clip, live transcript, end session.

import { api } from "../api.js";
import { subscribe } from "../sse.js";
import { ClipRecorder, HARD_CAP_MS } from "../recorder.js";
import { enqueueClip, listBySession, onChange, pendingForSession } from "../outbox.js";
import { el, clear, shortId, fmtDuration, badge, confirmDialog } from "../util.js";

export async function mountActive(root, sessionId) {
  clear(root);

  // Initial state.
  let sessionRow = null;
  const clipsByClipId = new Map();   // server-side clip rows
  const outboxByClipId = new Map();  // local outbox entries
  let sseHandle = null;
  let recorder = null;
  let elapsedTimer = null;
  let unsubscribeOutbox = null;
  let nextSeq = 0;
  let leaving = false;

  try {
    const res = await api.getSession(sessionId);
    sessionRow = res.session;
    for (const c of (res.clips || [])) clipsByClipId.set(c.clip_id, c);
    nextSeq = res.clips ? res.clips.length : 0;
  } catch (err) {
    root.append(el("p", { class: "error", text: `Failed to load session: ${err.message}` }));
    return () => {};
  }

  // Merge outbox entries we already have for this session.
  for (const e of await listBySession(sessionId)) {
    outboxByClipId.set(e.clip_id, e);
    if (typeof e.seq === "number" && e.seq + 1 > nextSeq) nextSeq = e.seq + 1;
  }

  // Build DOM.
  const elapsedNode = el("span", { class: "elapsed", text: "0:00" });
  const stateBadge = badge(sessionRow.state);
  const header = el("div", { class: "toolbar" },
    el("h1", { text: `Session ${shortId(sessionId)}` }),
    el("span", { class: "muted", text: `template: ${sessionRow.template_id}` }),
    stateBadge,
    el("div", { class: "spacer" }),
    elapsedNode,
  );

  const clipIndicator = el("span", { class: "muted", text: "" });
  const recBtn = el("button", { class: "btn-primary btn-big", text: "Start clip" });
  const endBtn = el("button", { class: "btn-danger", text: "End session" });
  const recordingBar = el("div", { class: "toolbar" }, recBtn, clipIndicator, el("div", { class: "spacer" }), endBtn);

  const liveTranscript = el("div", { class: "transcript-pane", text: "" });
  const clipList = el("ul", { class: "clip-list" });

  root.append(header, recordingBar,
    el("h2", { text: "Rolling transcript" }), liveTranscript,
    el("h2", { text: "Clips" }), clipList,
  );

  // Session wall-clock elapsed (recorder takes over while a clip is recording).
  const sessionStartMs = Date.parse(sessionRow.started_at) || Date.now();
  const tickWall = () => {
    if (recorder && recorder.isRunning()) return;
    elapsedNode.textContent = fmtDuration(Date.now() - sessionStartMs);
  };
  elapsedTimer = setInterval(tickWall, 1000);
  tickWall();

  function renderClips() {
    clear(clipList);
    // Merge server clips and outbox entries by clip_id; sort by seq.
    const merged = new Map();
    for (const c of clipsByClipId.values()) {
      merged.set(c.clip_id, { ...c, _local: outboxByClipId.get(c.clip_id) || null });
    }
    for (const e of outboxByClipId.values()) {
      if (!merged.has(e.clip_id)) {
        merged.set(e.clip_id, {
          clip_id: e.clip_id,
          seq: e.seq,
          started_at: e.started_at,
          duration_ms: e.duration_ms,
          state: e.state, // queued/confirmed/failed/orphaned (local)
          transcript: null,
          _local: e,
        });
      }
    }
    const rows = Array.from(merged.values()).sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    if (rows.length === 0) {
      clipList.append(el("li", { class: "muted", text: "No clips yet." }));
      return;
    }
    for (const r of rows) {
      const serverState = clipsByClipId.has(r.clip_id) ? clipsByClipId.get(r.clip_id).state : null;
      const local = r._local;
      const localState = local ? local.state : null;
      const stateLabel = serverState || localState || "?";
      const li = el("li", {},
        el("div", { class: "clip-head" },
          el("span", { class: "mono", text: `#${r.seq} ${shortId(r.clip_id)}` }),
          badge(stateLabel),
          el("span", { class: "muted", text: fmtDuration(r.duration_ms || 0) }),
          local && local.attempts ? el("span", { class: "muted", text: `attempts: ${local.attempts}` }) : null,
          local && local.last_error ? el("span", { class: "muted", text: `err: ${local.last_error}` }) : null,
        ),
        r.transcript ? el("div", { class: "clip-transcript", text: r.transcript }) : null,
      );
      clipList.append(li);
    }
  }
  renderClips();

  function appendLiveTranscript(prefix, text) {
    if (!text) return;
    const line = `[${prefix}] ${text}\n`;
    liveTranscript.textContent += line;
    liveTranscript.scrollTop = liveTranscript.scrollHeight;
  }

  // SSE subscription. Replay-on-connect is fine.
  sseHandle = subscribe(sessionId, ({ type, row }) => {
    if (!row) return;
    if (type === "scribe.clip.transcribed.v1" && row.clip_id) {
      const existing = clipsByClipId.get(row.clip_id) || { clip_id: row.clip_id };
      const transcript = row.data && row.data.transcript;
      clipsByClipId.set(row.clip_id, { ...existing, state: "transcribed", transcript });
      appendLiveTranscript(shortId(row.clip_id), transcript);
      renderClips();
    } else if (type === "scribe.clip.received.v1" && row.clip_id) {
      const existing = clipsByClipId.get(row.clip_id) || { clip_id: row.clip_id, seq: row.data && row.data.seq };
      clipsByClipId.set(row.clip_id, { ...existing, state: existing.state === "transcribed" ? "transcribed" : "uploaded" });
      renderClips();
    } else if (type === "scribe.clip.failed.v1" && row.clip_id) {
      const existing = clipsByClipId.get(row.clip_id) || { clip_id: row.clip_id };
      clipsByClipId.set(row.clip_id, { ...existing, state: "failed", _reason: row.data && row.data.reason });
      renderClips();
    } else if (type === "scribe.session.assembled.v1" || type === "scribe.session.structured.v1") {
      stateBadge.replaceWith(badge("assembling"));
    } else if (type === "scribe.session.completed.v1" && !leaving) {
      leaving = true;
      setTimeout(() => { window.location.hash = `#/open/${sessionId}?tab=markdown`; }, 200);
    } else if (type === "scribe.session.failed.v1") {
      stateBadge.replaceWith(badge("failed"));
    }
  });

  unsubscribeOutbox = onChange((entry) => {
    if (entry.session_id !== sessionId) return;
    outboxByClipId.set(entry.clip_id, entry);
    renderClips();
  });

  // Recorder.
  recorder = new ClipRecorder({
    onTick: ({ elapsedMs, warning, autoRollImminent, hardCapMs }) => {
      elapsedNode.textContent = fmtDuration(elapsedMs);
      if (warning) {
        elapsedNode.classList.add("warn");
        const remaining = Math.max(0, hardCapMs - elapsedMs);
        clipIndicator.textContent = autoRollImminent
          ? "auto-rolling now..."
          : `auto-rolling at 10:00 (in ${fmtDuration(remaining)})`;
      } else {
        elapsedNode.classList.remove("warn");
        clipIndicator.textContent = "recording...";
      }
    },
    onClipFinalized: async (clip) => {
      // Persist to outbox; sync loop will upload.
      try {
        await enqueueClip({
          session_id: sessionId,
          clip_id: clip.clip_id,
          seq: clip.seq,
          started_at: clip.started_at,
          duration_ms: clip.duration_ms,
          audio_format: clip.audio_format,
          audio: clip.audio,
          blob: clip.blob,
          meta: clip.meta,
        });
      } catch (err) {
        console.error("enqueue failed", err);
        alert(`Failed to queue clip: ${err.message}`);
      }
    },
    onError: (err) => {
      console.error("recorder error", err);
      alert(`Recorder error: ${err && err.message ? err.message : err}`);
      recBtn.textContent = "Start clip";
    },
  });
  recorder.setSeq(nextSeq);

  recBtn.addEventListener("click", async () => {
    if (recorder.isRunning()) {
      recBtn.disabled = true;
      await recorder.stop({ userInitiated: true });
      recBtn.textContent = "Start clip";
      recBtn.disabled = false;
      clipIndicator.textContent = "";
      elapsedNode.classList.remove("warn");
    } else {
      try {
        await recorder.start();
        recBtn.textContent = "Stop clip";
      } catch (err) {
        alert(`Could not start recording: ${err && err.message ? err.message : err}`);
      }
    }
  });

  endBtn.addEventListener("click", async () => {
    endBtn.disabled = true;
    try {
      if (recorder.isRunning()) await recorder.stop({ userInitiated: true });
      // Wait up to 30s for queued clips to confirm.
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const pending = await pendingForSession(sessionId);
        if (pending === 0) break;
        await new Promise((r) => setTimeout(r, 500));
      }
      const stillPending = await pendingForSession(sessionId);
      if (stillPending > 0) {
        const ok = await confirmDialog(`${stillPending} clip(s) still uploading. Wait or close anyway?`);
        if (!ok) { endBtn.disabled = false; return; }
      }
      await api.closeSession(sessionId, "user");
      leaving = true;
      window.location.hash = `#/open/${sessionId}`;
    } catch (err) {
      endBtn.disabled = false;
      alert(`Failed to end session: ${err.message}`);
    }
  });

  // Teardown.
  return () => {
    leaving = true;
    if (sseHandle) sseHandle.close();
    if (elapsedTimer) clearInterval(elapsedTimer);
    if (unsubscribeOutbox) unsubscribeOutbox();
    if (recorder) recorder.dispose();
  };
}
