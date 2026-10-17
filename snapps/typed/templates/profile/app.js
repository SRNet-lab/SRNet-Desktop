"use strict";
function render(v) {
  const r = tk.root(); r.appendChild(tk.el("h2", null, "People · " + v.collection));
  r.appendChild(tk.el("div", "muted", "Names are not unique: always check the key suffix after the dot."));
  for (const p of v.view.people || []) {
    const d = tk.el("div", "item"); d.appendChild(tk.el("span", "who", p.label));
    if (p.bio) d.appendChild(tk.el("div", "body", p.bio));
    d.appendChild(tk.el("div", "muted", "key " + p.key.slice(0, 16) + "…" + (p.recovery_committed ? " · recovery key committed" : " · no recovery key") + (p.current !== p.key ? (p.current ? " · rotated to " + String(p.current).slice(0, 12) + "…" : " · retired") : "")));
    r.appendChild(d);
  }
  r.appendChild(tk.el("h3", null, "Your profile"));
  const n = tk.el("input"); n.placeholder = "display name"; n.maxLength = 32; const b = tk.el("textarea"); b.placeholder = "bio"; b.maxLength = 280;
  r.appendChild(n); r.appendChild(b);
  r.appendChild(tk.btn("Publish profile", () => tk.draft("id.profile", {name: n.value, bio: b.value || null})));
}
tk.onView(render);
