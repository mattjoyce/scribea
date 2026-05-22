// New session — template picker.

import { api } from "../api.js";
import { el, clear } from "../util.js";

export async function mountNew(root) {
  clear(root);
  root.append(el("h1", { text: "Pick a template" }));

  let templates = [];
  try {
    templates = await api.templates();
  } catch (err) {
    root.append(el("p", { class: "error", text: `Failed to load templates: ${err.message}` }));
    return () => {};
  }

  if (!templates || templates.length === 0) {
    root.append(el("p", { class: "empty", text: "No templates available." }));
    return () => {};
  }

  const grid = el("div", { class: "cards" });
  for (const t of templates) {
    const card = el("div", { class: "card", role: "button", tabIndex: "0" },
      el("h3", { text: t.name || t.template_id }),
      el("p", { text: t.description || "" }),
      el("p", { class: "muted", text: `${t.template_id} • v${t.version || "?"} • prompt ${t.prompt_id || "?"}@${t.prompt_version || "?"}` }),
    );
    const onPick = async () => {
      card.style.opacity = "0.5";
      try {
        const sess = await api.createSession(t.template_id, {
          user_agent: navigator.userAgent,
          pwa_version: "v0",
        });
        window.location.hash = `#/active/${sess.session_id}`;
      } catch (err) {
        card.style.opacity = "";
        alert(`Failed to create session: ${err.message}`);
      }
    };
    card.addEventListener("click", onPick);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(); }
    });
    grid.append(card);
  }
  root.append(grid);

  return () => {};
}
