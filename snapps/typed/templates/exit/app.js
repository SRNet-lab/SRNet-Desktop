"use strict";
function render(v) {
  const r = tk.root(); const w = v.view;
  r.appendChild(tk.el("h2", null, "Exit board · " + v.collection));
  r.appendChild(tk.el("div", "muted", "Signed exit board (existing format, verified by the core) · seq " + w.seq + (w.dropped && w.dropped.length ? " · " + w.dropped.length + " listings dropped" : "")));
  for (const l of w.listings || []) {
    const d = tk.el("div", "item"); d.appendChild(tk.el("span", "who", String(l.seller).slice(0, 20) + "…"));
    for (const [k, val] of Object.entries(l.fields)) d.appendChild(tk.el("div", "muted", k + ": " + val));
    r.appendChild(d);
  }
}
tk.onView(render);
