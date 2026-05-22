// Sessions list — table of all sessions newest first.

import { api } from "../api.js";
import { el, clear, shortId, fmtTime, fmtDuration, badge } from "../util.js";

const OPEN_LIKE = new Set(["open", "recording"]);

function durationOf(row) {
  if (!row.started_at) return null;
  const start = Date.parse(row.started_at);
  const end = row.closed_at ? Date.parse(row.closed_at) : Date.now();
  return end - start;
}

export async function mountList(root) {
  clear(root);
  root.append(
    el("div", { class: "toolbar" },
      el("h1", { text: "Sessions" }),
      el("div", { class: "spacer" }),
      el("a", { class: "btn btn-primary", href: "#/new", text: "New session" }),
    ),
  );

  let sessions = [];
  let clipCounts = {};
  try {
    sessions = await api.listSessions(100);
  } catch (err) {
    root.append(el("p", { class: "error", text: `Failed to load sessions: ${err.message}` }));
    return () => {};
  }

  if (!sessions || sessions.length === 0) {
    root.append(el("p", { class: "empty", text: "No sessions yet. Tap New session to begin." }));
    return () => {};
  }

  // Fire clip-count lookups in parallel; tolerate failures.
  const counts = await Promise.all(sessions.map((s) =>
    api.listClips(s.session_id).then((cs) => [s.session_id, cs.length]).catch(() => [s.session_id, "?"])
  ));
  for (const [id, n] of counts) clipCounts[id] = n;

  const table = el("table",
    {},
    el("thead", {},
      el("tr", {},
        el("th", { text: "id" }),
        el("th", { text: "template" }),
        el("th", { text: "started" }),
        el("th", { text: "state" }),
        el("th", { text: "duration" }),
        el("th", { text: "clips" }),
      ),
    ),
  );
  const tbody = el("tbody", {});
  for (const s of sessions) {
    const target = OPEN_LIKE.has(s.state) ? `#/active/${s.session_id}` : `#/open/${s.session_id}`;
    const tr = el("tr", { class: "row-link", dataset: { href: target } },
      el("td", { class: "mono", text: shortId(s.session_id) }),
      el("td", { text: s.template_id }),
      el("td", { text: fmtTime(s.started_at) }),
      el("td", {}, badge(s.state)),
      el("td", { text: fmtDuration(durationOf(s)) }),
      el("td", { text: String(clipCounts[s.session_id] ?? "?") }),
    );
    tr.addEventListener("click", () => { window.location.hash = target; });
    tbody.append(tr);
  }
  table.append(tbody);
  root.append(table);

  return () => {};
}
