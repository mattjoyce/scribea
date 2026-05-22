// Hash router + screen mounting.

import { mountList } from "./screens/list.js";
import { mountNew } from "./screens/new.js";
import { mountActive } from "./screens/active.js";
import { mountOpen } from "./screens/open.js";
import { startSyncLoop } from "./outbox.js";
import { clear, setStatusPill } from "./util.js";

let currentTeardown = null;

function parseHash() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const parts = path.split("/").filter(Boolean);
  return { path, parts, params, raw };
}

async function route() {
  // Tear down previous screen (closes EventSource, stops recorder, etc.)
  if (typeof currentTeardown === "function") {
    try { currentTeardown(); } catch (e) { console.error("teardown error", e); }
    currentTeardown = null;
  }
  const app = document.getElementById("app");
  clear(app);

  const { parts, params } = parseHash();

  try {
    if (parts.length === 0) {
      currentTeardown = await mountList(app);
    } else if (parts[0] === "new") {
      currentTeardown = await mountNew(app);
    } else if (parts[0] === "active" && parts[1]) {
      currentTeardown = await mountActive(app, parts[1]);
    } else if (parts[0] === "open" && parts[1]) {
      currentTeardown = await mountOpen(app, parts[1], params.get("tab") || "baggage");
    } else {
      app.innerHTML = `<p class="empty">Unknown route: <span class="mono">${parts.join("/")}</span></p>`;
    }
  } catch (err) {
    console.error("route error", err);
    app.innerHTML = `<p class="error">${err && err.message ? err.message : String(err)}</p>`;
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => {
  // Register service worker (best-effort).
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("sw register failed", e));
  }
  // Use replaceState (not assignment) — setting `window.location.hash` fires
  // `hashchange`, which would call route() and double-mount the initial screen.
  if (!window.location.hash) history.replaceState(null, "", "#/");
  setStatusPill("");
  startSyncLoop();
  route();
});

// Tear down on full unload too (best-effort).
window.addEventListener("beforeunload", () => {
  if (typeof currentTeardown === "function") {
    try { currentTeardown(); } catch { /* ignore */ }
  }
});
