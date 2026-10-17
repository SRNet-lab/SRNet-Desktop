"use strict";
let open = null, editing = false;
function render(v) {
  const r = tk.root(); const pages = v.view.pages || {};
  r.appendChild(tk.el("h2", null, "Wiki · " + v.collection));
  if (!open) {
    for (const s of Object.keys(pages).sort()) { const p = pages[s]; const d = tk.el("div", "item"); d.appendChild(tk.btn(p.title, () => { open = s; render(v); })); d.appendChild(tk.el("span", "muted", (p.protected ? " 🔒 protected" : "") + " · " + p.history.length + " revisions")); r.appendChild(d); }
    const slug = tk.el("input"); slug.placeholder = "new-page-slug"; const title = tk.el("input"); title.placeholder = "Title";
    r.appendChild(slug); r.appendChild(title); r.appendChild(tk.btn("Create page", () => tk.draft("wiki.page", {slug: slug.value, title: title.value})));
    return;
  }
  const p = pages[open] || {slug: open, title: open, head: null, text: "", history: [], conflicts: []};
  r.appendChild(tk.btn("← all pages", () => { open = null; editing = false; render(v); }));
  r.appendChild(tk.el("h3", null, p.title + (p.protected ? " 🔒" : "")));
  if (editing) {
    const ta = tk.el("textarea"); ta.rows = 14; ta.value = p.text; const sum = tk.el("input"); sum.placeholder = "edit summary";
    r.appendChild(ta); r.appendChild(sum);
    r.appendChild(tk.btn("Save revision", () => { tk.draft("wiki.rev", {slug: p.slug, text: ta.value, summary: sum.value || null}, p.head ? [p.head] : []); editing = false; }));
    r.appendChild(tk.btn("Cancel", () => { editing = false; render(v); }));
  } else {
    r.appendChild(tk.el("div", "body", p.text || "(empty)"));
    r.appendChild(tk.btn("Edit", () => { editing = true; render(v); }));
  }
  r.appendChild(tk.el("h3", null, "History"));
  for (const h of p.history.slice().reverse()) { const d = tk.el("div", "item muted", h.t + " · " + h.by + " · " + tk.when(h.ts) + (h.summary ? " · " + h.summary : "")); if (h.id !== p.head) d.appendChild(tk.btn("Revert to this", () => tk.draft("wiki.revert", {slug: p.slug, summary: "revert"}, [h.id]))); r.appendChild(d); }
  if (p.conflicts.length) r.appendChild(tk.el("div", "warn", p.conflicts.length + " edit(s) not applied (stale parent or protected)"));
}
tk.onView(render);
