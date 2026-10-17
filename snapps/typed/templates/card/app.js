"use strict";
function render(v) {
  const r = tk.root(); const w = v.view;
  r.appendChild(tk.el("h2", null, "SNApp card"));
  r.appendChild(tk.el("div", w.ok ? "ok" : "warn", w.ok ? "Signature and expiry verified by the core" : "NOT VERIFIED: " + (w.errors || []).join("; ")));
  for (const [k, val] of Object.entries(w.card || {})) r.appendChild(tk.el("div", "muted", k + ": " + val));
}
tk.onView(render);
