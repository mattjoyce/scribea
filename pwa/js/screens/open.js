// Open session — pipeline-shaped read-only view. Eight tabs walk the pipeline
// from clip upload through to final note, plus a baggage tab for the raw event
// log. Per-clip data (audio, entities, redactions) is fetched as part of the
// session payload; per-session data (assembled context, structured, markdown)
// is fetched lazily when its tab is opened.

import { api } from "../api.js";
import { subscribe } from "../sse.js";
import {
  el, clear, shortId, fmtTime, fmtDuration, badge, mdToHtml,
  jsonTree, highlightSpans,
} from "../util.js";

const TABS = ["clips", "context", "ner", "redact", "transcript", "structured", "note", "baggage"];

// Per-clip lifecycle stages, in pipeline order. Used to render a dot strip on
// each clip card showing how far down the pipeline that clip has progressed.
const CLIP_STAGES = ["received", "preprocessed", "transcribed", "entities", "redacted"];

export async function mountOpen(root, sessionId, initialTab) {
  clear(root);

  let sseHandle = null;
  let activeTab = TABS.includes(initialTab) ? initialTab : "clips";
  let baggage = null; // cached event list once fetched

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

  const totalMs = clips.reduce((acc, c) => acc + (c.duration_ms || 0), 0);
  const summaryLine = `${clips.length} clip${clips.length === 1 ? "" : "s"} · ${fmtDuration(totalMs)} total`;

  const header = el("div", { class: "toolbar" },
    el("h1", { text: `Session ${shortId(sessionId)}` }),
    badge(sessionRow.state),
    el("span", { class: "muted", text: summaryLine }),
    el("div", { class: "spacer" }),
  );
  const summary = el("dl", { class: "kv" },
    el("dt", { text: "template" }), el("dd", { text: sessionRow.template_id }),
    el("dt", { text: "started" }), el("dd", { text: fmtTime(sessionRow.started_at) }),
    el("dt", { text: "closed" }), el("dd", { text: sessionRow.closed_at ? fmtTime(sessionRow.closed_at) : "—" }),
    el("dt", { text: "close_reason" }), el("dd", { text: sessionRow.close_reason || "—" }),
    el("dt", { text: "case_id" }), el("dd", { text: sessionRow.case_id || "—" }),
  );
  root.append(header, summary);

  const tabsRow = el("div", { class: "tabs" });
  const tabPanel = el("div", {});
  for (const t of TABS) {
    const btn = el("button", { class: `tab ${t === activeTab ? "active" : ""}`, text: t });
    btn.addEventListener("click", () => {
      activeTab = t;
      const url = `#/open/${sessionId}?tab=${t}`;
      if (window.location.hash !== url) {
        window.location.hash = url;
      } else {
        renderTab();
      }
    });
    tabsRow.append(btn);
  }
  root.append(tabsRow, tabPanel);

  async function refreshClips() {
    try {
      clips = await api.listClips(sessionId);
    } catch (err) {
      // Keep stale clips on failure rather than wiping the view.
      console.warn("refreshClips failed", err);
    }
  }

  async function ensureBaggage() {
    if (baggage !== null) return baggage;
    try {
      baggage = await api.getBaggage(sessionId);
    } catch (err) {
      baggage = { __error: err.message };
    }
    return baggage;
  }

  async function renderTab() {
    clear(tabPanel);
    if (activeTab === "clips") return renderClipsTab(tabPanel, sessionId, clips);
    if (activeTab === "context") return renderContextTab(tabPanel, await ensureBaggage());
    if (activeTab === "ner") return renderNerTab(tabPanel, clips);
    if (activeTab === "redact") return renderRedactTab(tabPanel, clips);
    if (activeTab === "transcript") return renderTranscriptTab(tabPanel, await ensureBaggage());
    if (activeTab === "structured") return renderStructuredTab(tabPanel, sessionId);
    if (activeTab === "note") return renderNoteTab(tabPanel, sessionId);
    if (activeTab === "baggage") return renderBaggageTab(tabPanel, await ensureBaggage());
  }
  await renderTab();

  // Live updates: refetch clips on any per-clip stage event and re-render the
  // current tab. For session-level events that finalise data, invalidate the
  // baggage cache so the relevant tab gets a fresh fetch on next render.
  if (sessionRow.state !== "completed" && sessionRow.state !== "failed" && sessionRow.state !== "abandoned") {
    sseHandle = subscribe(sessionId, async ({ type }) => {
      if (!type) return;
      if (type.startsWith("scribe.clip.")) {
        await refreshClips();
      }
      if (
        type === "scribe.case.context_attached.v1" ||
        type === "scribe.session.assembled.v1" ||
        type === "scribe.session.structured.v1" ||
        type === "scribe.session.completed.v1" ||
        type === "scribe.session.failed.v1"
      ) {
        baggage = null;
      }
      renderTab();
    });
  }

  return () => {
    if (sseHandle) sseHandle.close();
  };
}

// ---------- Clips ----------

function renderClipsTab(panel, sessionId, clips) {
  if (clips.length === 0) {
    panel.append(el("p", { class: "muted", text: "No clips uploaded." }));
    return;
  }
  const list = el("ul", { class: "clip-list" });
  for (const c of clips) {
    list.append(renderClipCard(sessionId, c));
  }
  panel.append(list);
}

function renderClipCard(sessionId, c) {
  const meta = parseMeta(c.meta);
  const stagesReached = clipStagesReached(c, meta);
  const stagesFailed = clipStagesFailed(meta);

  const stageStrip = el("div", { class: "stage-strip" });
  for (const stage of CLIP_STAGES) {
    let cls = "stage-dot";
    if (stagesFailed.has(stage)) cls += " failed";
    else if (stagesReached.has(stage)) cls += " done";
    stageStrip.append(el("span", { class: cls, title: stage, text: stage }));
  }

  const audio = el("audio", { controls: true, preload: "none", src: api.clipAudioUrl(sessionId, c.clip_id) });

  const head = el("div", { class: "clip-head" },
    el("span", { class: "mono", text: `#${c.seq} ${shortId(c.clip_id)}` }),
    badge(c.state),
    el("span", { class: "muted", text: fmtDuration(c.duration_ms) }),
  );

  const li = el("li", {}, head, stageStrip, audio);
  if (c.transcript) {
    const tx = el("details", { class: "clip-detail" },
      el("summary", { text: "transcript" }),
      el("div", { class: "clip-transcript", text: c.transcript }),
    );
    li.append(tx);
  }
  return li;
}

function parseMeta(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try { return JSON.parse(raw); } catch { return {}; }
}

function clipStagesReached(c, meta) {
  const reached = new Set(["received"]);
  if (meta && meta.preprocessing) reached.add("preprocessed");
  if (c.transcript != null || (meta && meta.transcribe)) reached.add("transcribed");
  if (c.entities != null) reached.add("entities");
  if (c.redacted_transcript_ref != null || c.redactions != null) reached.add("redacted");
  return reached;
}

function clipStagesFailed(meta) {
  const failed = new Set();
  if (!meta) return failed;
  if (meta.preprocess_failed) failed.add("preprocessed");
  if (meta.transcribe_failed) failed.add("transcribed");
  if (meta.ner_failed) failed.add("entities");
  if (meta.redact_failed) failed.add("redacted");
  return failed;
}

// ---------- Context ----------

function renderContextTab(panel, baggage) {
  panel.append(el("p", { class: "muted", text: "EMR backstory snapshot attached at session start." }));
  if (baggage && baggage.__error) {
    panel.append(el("p", { class: "error", text: `Failed to load baggage: ${baggage.__error}` }));
    return;
  }
  const evt = findLatestEvent(baggage, "scribe.case.context_attached.v1");
  if (!evt) {
    panel.append(el("p", { class: "muted", text: "No context attached to this session." }));
    return;
  }
  const payload = unwrapData(evt);
  panel.append(jsonTree(payload, { openDepth: 2 }));
}

// ---------- NER ----------

function renderNerTab(panel, clips) {
  panel.append(el("p", { class: "muted", text: "Named-entity hits per clip. Toggle the highlight to inspect spans inline." }));
  const withNer = clips.filter((c) => Array.isArray(c.entities));
  if (withNer.length === 0) {
    panel.append(el("p", { class: "muted", text: "NER has not produced output for any clip yet." }));
    return;
  }
  for (const c of withNer) {
    panel.append(renderClipAnnotated(c, c.entities, "ner"));
  }
}

// ---------- Redact ----------

function renderRedactTab(panel, clips) {
  panel.append(el("p", { class: "muted", text: "PHI redactions per clip. Highlights show what was stripped on the raw transcript." }));
  const withRedact = clips.filter((c) => Array.isArray(c.redactions));
  if (withRedact.length === 0) {
    panel.append(el("p", { class: "muted", text: "Redactor has not produced output for any clip yet." }));
    return;
  }
  for (const c of withRedact) {
    panel.append(renderClipAnnotated(c, c.redactions, "redact"));
  }
}

// renderClipAnnotated renders a per-clip annotation card used by both the NER
// and Redact tabs. spans[] uses the same {start, end, label, ...} contract for
// both — phiCategory in util.js handles the colour palette.
function renderClipAnnotated(c, spans, mode) {
  const header = el("div", { class: "clip-head" },
    el("span", { class: "mono", text: `#${c.seq} ${shortId(c.clip_id)}` }),
    el("span", { class: "muted", text: `${spans.length} span${spans.length === 1 ? "" : "s"}` }),
  );

  const text = c.transcript || "";
  const textPane = el("div", { class: "annotated-text" });
  const renderHighlighted = () => {
    clear(textPane);
    if (!text) {
      textPane.append(el("span", { class: "muted", text: "no transcript on this clip" }));
      return;
    }
    textPane.append(highlightSpans(text, spans));
  };
  const renderPlain = () => {
    clear(textPane);
    textPane.append(document.createTextNode(text || "—"));
  };
  renderHighlighted();

  const toggleRow = el("div", { class: "toggle-row" });
  const btnHighlight = el("button", { class: "tab-mini active", text: "highlight" });
  const btnPlain = el("button", { class: "tab-mini", text: "plain" });
  btnHighlight.addEventListener("click", () => {
    btnHighlight.classList.add("active"); btnPlain.classList.remove("active");
    renderHighlighted();
  });
  btnPlain.addEventListener("click", () => {
    btnPlain.classList.add("active"); btnHighlight.classList.remove("active");
    renderPlain();
  });
  toggleRow.append(btnHighlight, btnPlain);

  const card = el("div", { class: "clip-annotated" }, header, toggleRow, textPane);

  // Span list as a collapsible table.
  const tbl = el("table", { class: "span-table" },
    el("thead", {}, el("tr", {},
      el("th", { text: mode === "ner" ? "type" : "label" }),
      el("th", { text: "text" }),
      el("th", { text: "start" }),
      el("th", { text: "end" }),
      el("th", { text: "score" }),
    )),
  );
  const tbody = el("tbody", {});
  for (const s of spans) {
    tbody.append(el("tr", {},
      el("td", { text: s.label || s.entity_type || "" }),
      el("td", { class: "mono", text: s.text || s.original_text || (text ? text.slice(s.start, s.end) : "") }),
      el("td", { class: "mono", text: String(s.start ?? "") }),
      el("td", { class: "mono", text: String(s.end ?? "") }),
      el("td", { class: "mono", text: typeof s.score === "number" ? s.score.toFixed(3) : "" }),
    ));
  }
  tbl.append(tbody);
  const tblWrap = el("details", { class: "spans-details" },
    el("summary", { text: `${spans.length} span${spans.length === 1 ? "" : "s"}` }),
    tbl,
  );
  card.append(tblWrap);

  if (mode === "redact" && c.redacted_transcript_ref) {
    card.append(el("p", { class: "muted mono", text: `redacted_transcript_ref: ${c.redacted_transcript_ref}` }));
  }
  return card;
}

// ---------- Transcript (assembled) ----------

function renderTranscriptTab(panel, baggage) {
  panel.append(el("p", { class: "muted", text: "Assembled context handed to the LLM — clip transcripts + EMR backstory, in order." }));
  if (baggage && baggage.__error) {
    panel.append(el("p", { class: "error", text: `Failed to load baggage: ${baggage.__error}` }));
    return;
  }
  const evt = findLatestEvent(baggage, "scribe.session.assembled.v1");
  if (!evt) {
    panel.append(el("p", { class: "muted", text: "Session has not been assembled yet." }));
    return;
  }
  const payload = unwrapData(evt);
  // Common assembled-event field names — fall back to JSON tree if none match.
  const text = payload.assembled_context || payload.context || payload.text || payload.transcript || null;
  if (text) {
    panel.append(el("pre", { class: "json", text: String(text) }));
  }
  const more = el("details", {}, el("summary", { text: "full event payload" }));
  more.append(jsonTree(payload, { openDepth: 1 }));
  panel.append(more);
}

// ---------- Structured ----------

async function renderStructuredTab(panel, sessionId) {
  try {
    const note = await api.getNote(sessionId);
    const data = note.structured ?? note;
    panel.append(jsonTree(data, { openDepth: 2 }));
  } catch (err) {
    if (err.status === 404) {
      panel.append(el("p", { class: "muted", text: "Note not ready yet. Awaiting completion." }));
    } else {
      panel.append(el("p", { class: "error", text: `Failed to load note: ${err.message}` }));
    }
  }
}

// ---------- Note ----------

async function renderNoteTab(panel, sessionId) {
  try {
    const note = await api.getNote(sessionId);
    const md = note.markdown || "";
    panel.append(el("div", { class: "note-rendered", html: mdToHtml(md) }));
    panel.append(el("details", {},
      el("summary", { text: "raw markdown" }),
      el("pre", { class: "markdown", text: md }),
    ));
  } catch (err) {
    if (err.status === 404) {
      panel.append(el("p", { class: "muted", text: "Note not ready yet. Awaiting completion." }));
    } else {
      panel.append(el("p", { class: "error", text: `Failed to load note: ${err.message}` }));
    }
  }
}

// ---------- Baggage ----------

function renderBaggageTab(panel, baggage) {
  panel.append(el("p", { class: "muted", text: "Full event log for this session, oldest first." }));
  if (baggage && baggage.__error) {
    panel.append(el("p", { class: "error", text: `Failed to load baggage: ${baggage.__error}` }));
    return;
  }
  panel.append(jsonTree(baggage, { openDepth: 1 }));
}

// ---------- Helpers ----------

function findLatestEvent(baggage, type) {
  const events = baggageEvents(baggage);
  let latest = null;
  for (const e of events) {
    if ((e.event_type || e.type) === type) latest = e;
  }
  return latest;
}

// baggageEvents normalises whatever shape /baggage returns to a flat array.
// The endpoint currently returns the events array directly, but stay tolerant
// of a `{events: [...]}` wrapper too.
function baggageEvents(baggage) {
  if (Array.isArray(baggage)) return baggage;
  if (baggage && Array.isArray(baggage.events)) return baggage.events;
  return [];
}

// unwrapData returns the inner payload of an event row. Event rows have a
// `data` JSON column; if it's a string, parse it. Fall back to the event row
// itself for forward-compat with shapes that inline the payload.
function unwrapData(evt) {
  if (!evt) return {};
  if (evt.data == null) return evt;
  if (typeof evt.data === "string") {
    try { return JSON.parse(evt.data); } catch { return evt; }
  }
  return evt.data;
}
