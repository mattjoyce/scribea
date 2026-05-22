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
