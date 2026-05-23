// Utility helpers: id-shortening, time formatters, DOM helpers, minimal markdown.

export const shortId = (id) => (id ? String(id).slice(0, 8) : "");

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fmtDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return "";
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "dataset" && typeof v === "object") {
      for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
    } else {
      node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function badge(state) {
  return el("span", { class: `pill ${state || ""}` }, state || "?");
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

// Very basic markdown -> HTML. Supports headings h1-h6, paragraphs, fenced code, ul/ol.
// Anything fancier is rendered as a preformatted block by caller.
export function mdToHtml(md) {
  if (md == null) return "";
  const src = String(md).replace(/\r\n/g, "\n");
  const lines = src.split("\n");
  const out = [];
  let i = 0;
  let inCode = false;
  let codeBuf = [];
  let listType = null;
  let listBuf = [];

  const flushList = () => {
    if (!listType) return;
    out.push(`<${listType}>` + listBuf.map((t) => `<li>${inlineMd(t)}</li>`).join("") + `</${listType}>`);
    listType = null;
    listBuf = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    if (inCode) {
      if (line.trim().startsWith("```")) {
        out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
        codeBuf = [];
        inCode = false;
      } else {
        codeBuf.push(line);
      }
      i++; continue;
    }
    if (line.trim().startsWith("```")) {
      flushList();
      inCode = true;
      i++; continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushList();
      const lvl = h[1].length;
      out.push(`<h${lvl}>${inlineMd(h[2])}</h${lvl}>`);
      i++; continue;
    }
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul) {
      if (listType !== "ul") { flushList(); listType = "ul"; }
      listBuf.push(ul[1]);
      i++; continue;
    }
    if (ol) {
      if (listType !== "ol") { flushList(); listType = "ol"; }
      listBuf.push(ol[1]);
      i++; continue;
    }
    if (line.trim() === "") {
      flushList();
      i++; continue;
    }
    flushList();
    // paragraph: collect consecutive non-empty lines
    const para = [line];
    while (i + 1 < lines.length && lines[i + 1].trim() !== "" && !lines[i + 1].match(/^(#{1,6}\s|```|\s*[-*]\s|\s*\d+\.\s)/)) {
      i++;
      para.push(lines[i]);
    }
    out.push(`<p>${inlineMd(para.join(" "))}</p>`);
    i++;
  }
  flushList();
  if (inCode) out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
  return out.join("\n");
}

function inlineMd(s) {
  let t = escapeHtml(s);
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return t;
}

export function setStatusPill(msg) {
  const node = document.getElementById("status-pill");
  if (node) node.textContent = msg || "";
}

export function confirmDialog(text) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("confirm-dialog");
    if (!dlg || typeof dlg.showModal !== "function") return resolve(window.confirm(text));
    document.getElementById("confirm-text").textContent = text;
    const onClose = () => {
      dlg.removeEventListener("close", onClose);
      resolve(dlg.returnValue === "ok");
    };
    dlg.addEventListener("close", onClose);
    dlg.showModal();
  });
}

// jsonTree renders a value as a collapsible <details>-based tree. openDepth
// controls how deep the tree opens by default (1 = root expanded, children
// collapsed).
export function jsonTree(value, opts = {}) {
  const openDepth = opts.openDepth ?? 1;
  const root = el("div", { class: "json-tree" });
  root.append(renderJsonNode(value, "", 0, openDepth));
  return root;
}

function renderJsonNode(v, key, depth, openDepth) {
  if (v === null || v === undefined) return jsonScalarLine(key, "null", "json-null");
  const t = typeof v;
  if (t === "string") return jsonScalarLine(key, JSON.stringify(v), "json-string");
  if (t === "number") return jsonScalarLine(key, String(v), "json-number");
  if (t === "boolean") return jsonScalarLine(key, String(v), "json-boolean");
  if (Array.isArray(v)) {
    const head = key ? `${key}: ` : "";
    const summary = `${head}Array(${v.length})`;
    const node = el("details", { class: "json-node" });
    if (depth < openDepth) node.setAttribute("open", "");
    node.append(el("summary", { text: summary }));
    const body = el("div", { class: "json-children" });
    for (let i = 0; i < v.length; i++) body.append(renderJsonNode(v[i], `[${i}]`, depth + 1, openDepth));
    node.append(body);
    return node;
  }
  if (t === "object") {
    const keys = Object.keys(v);
    const head = key ? `${key}: ` : "";
    const summary = `${head}{${keys.length}}`;
    const node = el("details", { class: "json-node" });
    if (depth < openDepth) node.setAttribute("open", "");
    node.append(el("summary", { text: summary }));
    const body = el("div", { class: "json-children" });
    for (const k of keys) body.append(renderJsonNode(v[k], k, depth + 1, openDepth));
    node.append(body);
    return node;
  }
  return jsonScalarLine(key, String(v), "json-other");
}

function jsonScalarLine(key, valStr, cls) {
  const line = el("div", { class: "json-line" });
  if (key) line.append(el("span", { class: "json-key", text: `${key}: ` }));
  line.append(el("span", { class: cls, text: valStr }));
  return line;
}

// PHI / clinical label → broad category for the highlight palette. Keeps the
// colour count small (6 + other) so a transcript with many distinct labels
// stays readable. Unknown labels fall back to "other".
const PHI_CATEGORY_MAP = {
  person:     ["first_name", "last_name", "name", "patient", "doctor", "person", "clinician"],
  location:   ["city", "state", "zip_code", "address", "street_address", "country", "hospital", "ward", "location", "facility"],
  identifier: ["id_number", "mrn", "ssn", "account", "license", "case_number"],
  contact:    ["phone_number", "email", "url", "fax"],
  temporal:   ["date", "age", "time", "datetime", "dob"],
  clinical:   ["medication", "condition", "procedure", "anatomy", "dosage", "drug", "diagnosis", "symptom", "device"],
};

export function phiCategory(label) {
  if (!label) return "other";
  const l = String(label).toLowerCase();
  for (const [cat, labels] of Object.entries(PHI_CATEGORY_MAP)) {
    if (labels.includes(l)) return cat;
  }
  return "other";
}

// highlightSpans wraps `text` such that any (start, end) span gets a coloured
// background. Returns a DocumentFragment. Overlapping spans: first wins, later
// overlapping spans are dropped (no double-wrapping). Spans without a valid
// (start, end) pair are skipped.
export function highlightSpans(text, spans) {
  const frag = document.createDocumentFragment();
  if (!text) return frag;
  const sorted = (spans || [])
    .filter((s) => s && Number.isFinite(s.start) && Number.isFinite(s.end) && s.end > s.start)
    .slice()
    .sort((a, b) => a.start - b.start);
  let cursor = 0;
  for (const s of sorted) {
    if (s.start < cursor) continue;
    if (s.start > cursor) frag.append(document.createTextNode(text.slice(cursor, s.start)));
    const label = s.label || s.entity_type || "";
    const cat = phiCategory(label);
    const scorePart = typeof s.score === "number" ? ` (${(s.score * 100).toFixed(1)}%)` : "";
    frag.append(el("span", {
      class: `hl hl-${cat}`,
      title: `${label}${scorePart}`,
      text: text.slice(s.start, s.end),
    }));
    cursor = s.end;
  }
  if (cursor < text.length) frag.append(document.createTextNode(text.slice(cursor)));
  return frag;
}
