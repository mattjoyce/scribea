// Open session — read-only view + 3 tabs (baggage, structured, markdown).

import { api } from "../api.js";
import { subscribe } from "../sse.js";
import { el, clear, shortId, fmtTime, fmtDuration, badge, mdToHtml } from "../util.js";

const TABS = ["baggage", "structured", "markdown"];

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
