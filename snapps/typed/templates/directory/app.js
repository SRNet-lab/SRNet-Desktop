"use strict";
// Directory: hosts that opted in (each entry is signed by that collection's own root key).
// Join only asks the trusted shell to open its join sheet; nothing is joined from here.
const GROUPS = [
  ["room", "Rooms", "Join room"], ["drop", "File drops", "Join file drop"],
  ["mail", "Mail hosts", "Add mail host"], ["shop", "Stores", "Join store"],
  ["directory", "Directories", "Join directory"], ["wiki", "Wikis", "Join wiki"],
  ["board", "Boards", "Join board"], ["id", "Profiles", "Join"],
];
function render(v) {
  const r = tk.root(); r.appendChild(tk.el("h2", null, "Directory · " + v.collection));
  r.appendChild(tk.el("div", "muted", "Each entry is signed by the root key of the collection it lists. Joining shows the host's signed info first."));
  const es = (v.view && v.view.entries) || [];
  if (!es.length) r.appendChild(tk.el("div", "muted", "No listings yet."));
  const known = GROUPS.map((g) => g[0]);
  const groups = GROUPS.concat([["", "Other", "Join"]]);
  for (const [kind, title, label] of groups) {
    const list = es.filter((e) => kind ? e.kind === kind : !known.includes(e.kind));
    if (!list.length) continue;
    r.appendChild(tk.el("h3", null, title));
    for (const e of list) {
      const d = tk.el("div", "item");
      d.appendChild(tk.el("span", "who", e.title));
      d.appendChild(tk.el("span", "muted", " · " + e.kind + " · " + e.name));
      if (e.desc) d.appendChild(tk.el("div", "body", e.desc));
      d.appendChild(tk.el("div", "muted", "host " + String(e.host).slice(0, 16) + "…"));
      const b = tk.btn(label, () => tk.send({type: "join", link: e.link}));
      b.dataset.kind = e.kind;
      d.appendChild(b);
      r.appendChild(d);
    }
  }
  r.appendChild(tk.status(v));
}
tk.onView(render);
