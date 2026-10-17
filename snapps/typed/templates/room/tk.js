// Template toolkit (bundled into every template, hashed in the registry entry).
// Rules every template follows: render record data with textContent only; never touch the
// network (the CSP forbids it anyway); talk to the core only through tk.send().
"use strict";
const tk = {
  view: null,
  send(msg) { parent.postMessage(msg, "*"); },
  draft(t, body, refs) { tk.send({type: "draft", t, body, refs: refs || []}); },
  act(name, target) { tk.send({type: "action", name, target}); },
  el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = String(text); return e; },
  btn(label, fn) { const b = tk.el("button", null, label); b.addEventListener("click", fn); return b; },
  when(ts) { return new Date(ts * 1000).toLocaleString(); },
  // Host removals (signed, public, irreversible): a placeholder line, never the content.
  removedEl(x) { return tk.el("div", "item muted removed", "Removed by host: " + (x.reason || "no reason given")); },
  removed(r, v) { for (const x of (v.removed || [])) r.appendChild(tk.removedEl(x)); },
  // Host-signed header (description + rules) and up to 3 host pins, shown above the content.
  header(r, v) {
    const h = v.header; if (!h) return;
    if (h.description) r.appendChild(tk.el("div", "header-desc", h.description));
    if (h.rules) r.appendChild(tk.el("div", "muted header-rules", "Rules: " + h.rules));
    for (const p of v.pinned || []) {
      const d = tk.el("div", "item pinned"); d.appendChild(tk.el("span", "muted", "📌 pinned by host · "));
      d.appendChild(tk.el("span", "who", p.who + " ")); d.appendChild(tk.el("span", "body", p.text)); r.appendChild(d);
    }
  },
  root() { const r = document.getElementById("root"); r.replaceChildren(); return r; },
  onView(fn) {
    window.addEventListener("message", (ev) => {
      if (ev.source !== parent) return;
      const m = ev.data;
      if (m && m.type === "view") { tk.view = m.data; const keep = tk._snap(); fn(m.data); tk._restore(keep); }
    });
    tk.send({type: "ready"});
  },
  // Views re-render on every sync. Keep what the user is typing, and where, across renders:
  // fields are matched by data-k (or placeholder); a field the template filled keeps its value.
  _key(e) { return e.dataset.k || e.placeholder || e.name || ""; },
  _fields() { return Array.from(document.querySelectorAll("#root input, #root textarea, #root select")); },
  _snap() {
    const vals = {}, a = document.activeElement;
    for (const e of tk._fields()) { const k = tk._key(e); if (k && e.type !== "file") vals[k] = e.value; }
    const f = a && a !== document.body && tk._fields().includes(a) ? {k: tk._key(a), s: a.selectionStart, e: a.selectionEnd} : null;
    return {vals, f};
  },
  _restore(keep) {
    const els = tk._fields();
    for (const e of els) { const k = tk._key(e), v = keep.vals[k]; if (v && !e.value && e.type !== "file") e.value = v; }
    if (keep.f) {
      const e = els.find((x) => tk._key(x) === keep.f.k);
      if (e) { e.focus(); try { if (keep.f.s != null) e.setSelectionRange(keep.f.s, keep.f.e); } catch (err) { /* not a text field */ } }
    }
  },
  status(v) { const s = tk.el("div", "muted", (v.rules ? "Rules: " + v.rules : "")); return s; },
};
