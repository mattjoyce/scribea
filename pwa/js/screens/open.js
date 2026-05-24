// Open session — read-only view + 4 tabs (baggage, structured, markdown, diff).

import { api } from "../api.js";
import { subscribe } from "../sse.js";
import { el, clear, shortId, fmtTime, fmtDuration, badge, mdToHtml } from "../util.js";
import { tokenize, alignWords, werFromOps, renderDiffPair } from "../diff.js";

const TABS = ["baggage", "structured", "markdown", "diff"];
const GROUND_TRUTH_EVENT = "scribe.case.ground_truth_attached.v1";

export async function mountOpen(root, sessionId, initialTab) {
  clear(root);

  let sseHandle = null;
  let activeTab = TABS.includes(initialTab) ? initialTab : "baggage";

  let sessionRow = null;
  let clips = [];
  try {
    const res = await api.getSession(sessionId);
    sessionRow = res.session;
    clips = res.clips || [];
  } catch (err) {
    root.append(el("p", { class: "error", text: `Failed to load session: ${err.message}` }));
    return () => {};
  }

  const header = el("div", { class: "toolbar" },
    el("h1", { text: `Session ${shortId(sessionId)}` }),
    badge(sessionRow.state),
    el("div", { class: "spacer" }),
  );
  const summary = el("dl", { class: "kv" },
    el("dt", { text: "template" }), el("dd", { text: sessionRow.template_id }),
    el("dt", { text: "started" }), el("dd", { text: fmtTime(sessionRow.started_at) }),
    el("dt", { text: "closed" }), el("dd", { text: sessionRow.closed_at ? fmtTime(sessionRow.closed_at) : "—" }),
    el("dt", { text: "close_reason" }), el("dd", { text: sessionRow.close_reason || "—" }),
    el("dt", { text: "clips" }), el("dd", { text: String(clips.length) }),
  );
  root.append(header, summary);

  // Clip list (read-only).
  const clipsList = el("ul", { class: "clip-list" });
  if (clips.length === 0) {
    clipsList.append(el("li", { class: "muted", text: "No clips." }));
  } else {
    for (const c of clips) {
      clipsList.append(el("li", {},
        el("div", { class: "clip-head" },
          el("span", { class: "mono", text: `#${c.seq} ${shortId(c.clip_id)}` }),
          badge(c.state),
          el("span", { class: "muted", text: fmtDuration(c.duration_ms) }),
        ),
        c.transcript ? el("div", { class: "clip-transcript", text: c.transcript }) : null,
      ));
    }
  }
  root.append(el("h2", { text: "Clips" }), clipsList);

  // Tabs.
  const tabsRow = el("div", { class: "tabs" });
  const tabPanel = el("div", {});
  for (const t of TABS) {
    const btn = el("button", { class: `tab ${t === activeTab ? "active" : ""}`, text: t });
    btn.addEventListener("click", () => {
      activeTab = t;
      const url = `#/open/${sessionId}?tab=${t}`;
      if (window.location.hash !== url) {
        // Update without retriggering full route (browser will fire hashchange and we re-mount; that's fine).
        window.location.hash = url;
      } else {
        renderTab();
      }
    });
    tabsRow.append(btn);
  }
  root.append(el("h2", { text: "Inspect" }), tabsRow, tabPanel);

  async function renderTab() {
    clear(tabPanel);
    if (activeTab === "baggage") {
      tabPanel.append(el("p", { class: "muted", text: "Event log for this session." }));
      try {
        const events = await api.getBaggage(sessionId);
        tabPanel.append(el("pre", { class: "json", text: JSON.stringify(events, null, 2) }));
      } catch (err) {
        tabPanel.append(el("p", { class: "error", text: `Failed to load baggage: ${err.message}` }));
      }
    } else if (activeTab === "structured") {
      try {
        const note = await api.getNote(sessionId);
        tabPanel.append(el("pre", { class: "json", text: JSON.stringify(note.structured ?? note, null, 2) }));
      } catch (err) {
        if (err.status === 404) {
          tabPanel.append(el("p", { class: "muted", text: "Note not ready yet. Awaiting completion." }));
        } else {
          tabPanel.append(el("p", { class: "error", text: `Failed to load note: ${err.message}` }));
        }
      }
    } else if (activeTab === "markdown") {
      try {
        const note = await api.getNote(sessionId);
        const md = note.markdown || "";
        const rendered = el("div", { html: mdToHtml(md) });
        tabPanel.append(rendered);
        const details = el("details", {}, el("summary", { text: "raw markdown" }), el("pre", { class: "markdown", text: md }));
        tabPanel.append(details);
      } catch (err) {
        if (err.status === 404) {
          tabPanel.append(el("p", { class: "muted", text: "Note not ready yet. Awaiting completion." }));
        } else {
          tabPanel.append(el("p", { class: "error", text: `Failed to load note: ${err.message}` }));
        }
      }
    } else if (activeTab === "diff") {
      await renderDiffTab(tabPanel, sessionId);
    }
  }
  await renderTab();

  // Subscribe to SSE so the open view reacts when an in-flight session completes.
  // (Spec says open view is read-only but we still tail events for the baggage refresh
  // when a session transitions through assembling -> completed.)
  if (sessionRow.state !== "completed" && sessionRow.state !== "failed" && sessionRow.state !== "abandoned") {
    sseHandle = subscribe(sessionId, ({ type }) => {
      if (type === "scribe.session.completed.v1" || type === "scribe.session.failed.v1") {
        // Refresh tab content.
        renderTab();
      }
    });
  }

  return () => {
    if (sseHandle) sseHandle.close();
  };
}

// Diff tab: per-clip word-level alignment of ground-truth scripts (snapshot
// in the baggage event log by the harness at session-create time) vs. the
// ASR transcript. Refetches session + baggage on each render so transcripts
// stay fresh as the pipeline completes.
async function renderDiffTab(panel, sessionId) {
  panel.append(el("p", { class: "muted",
    text: "Per-clip diff of ground-truth script vs. ASR transcript (harnessed sessions only)." }));
  let res, events;
  try {
    [res, events] = await Promise.all([
      api.getSession(sessionId),
      api.getBaggage(sessionId),
    ]);
  } catch (err) {
    panel.append(el("p", { class: "error", text: `Failed to load: ${err.message}` }));
    return;
  }
  const gtEvent = (events || []).find((e) => e.event_type === GROUND_TRUTH_EVENT);
  if (!gtEvent) {
    panel.append(el("p", { class: "muted",
      text: "No ground truth attached — not a harnessed session." }));
    return;
  }
  const gtData = typeof gtEvent.data === "string" ? JSON.parse(gtEvent.data) : gtEvent.data;
  const truthBySeq = new Map();
  for (const c of (gtData.clips || [])) truthBySeq.set(c.seq, c.script || "");

  const clips = [...(res.clips || [])].sort((a, b) => a.seq - b.seq);
  if (clips.length === 0) {
    panel.append(el("p", { class: "muted", text: "No clips." }));
    return;
  }

  for (const c of clips) {
    const truthText = truthBySeq.get(c.seq) || "";
    const hypText = c.transcript || "";
    const truthTokens = tokenize(truthText);
    const hypTokens = tokenize(hypText);
    const ops = alignWords(truthTokens, hypTokens);
    const werRaw = werFromOps(ops, truthTokens.length);
    const werTxt = Number.isFinite(werRaw) ? `${(werRaw * 100).toFixed(1)}%` : "—";

    const head = el("div", { class: "diff-clip-head" },
      el("span", { class: "mono", text: `#${c.seq}` }),
      el("span", { class: "muted", text: `truth ${truthTokens.length} · hyp ${hypTokens.length}` }),
      el("span", { class: "diff-wer", text: `WER ${werTxt}` }),
    );
    const body = truthText
      ? renderDiffPair(ops)
      : el("p", { class: "muted", text: "No ground truth for this clip." });
    panel.append(el("div", { class: "diff-clip" }, head, body));
  }
}
