"use strict";
const MODE = {plain: "public, not encrypted", link: "encrypted · anyone with the link", gated: "encrypted · invited people only"};
function render(v) {
  const r = tk.root(); r.appendChild(tk.el("h2", null, "File drop · " + v.collection));
  r.appendChild(tk.el("div", "muted", "Use the upload bar above to share a file. Files are encrypted on your computer by default; the host stores only ciphertext. This page never sees file keys."));
  for (const it of v.view.items || []) {
    if (it.superseded_by) continue;
    const d = tk.el("div", "item");
    d.appendChild(tk.el("strong", null, it.encrypted ? (it.can_open ? "Encrypted file" : "Encrypted file (no access)") : it.name));
    d.appendChild(tk.el("div", "muted", MODE[it.mode] + " · " + (it.encrypted ? "~" : "") + it.size + " bytes · by " + it.by + (it.prev ? " · new version" : "") + (it.mine && it.encrypted ? " · shared with " + it.recipients + (it.revoked ? ", " + it.revoked + " revoked" : "") : "")));
    if (it.note) d.appendChild(tk.el("div", "body", it.note));
    if (it.can_open) d.appendChild(tk.btn("Open…", () => tk.send({type: "open", id: it.id})));
    if (it.mode === "link" && it.can_open) d.appendChild(tk.btn("Show link…", () => tk.send({type: "link", id: it.id})));
    if (!it.can_open && !it.mine) d.appendChild(it.requested ? tk.el("span", "muted", " access requested") : tk.btn("Request access…", () => tk.send({type: "request", id: it.id})));
    if (it.mine && it.encrypted) d.appendChild(tk.btn("Who can open…", () => tk.send({type: "access", id: it.id})));
    if (it.mine) d.appendChild(tk.btn("New version…", () => tk.send({type: "newver", id: it.id})));
    for (const q of it.requests || []) {
      const rq = tk.el("div", "muted", "Access request from " + q.by + (q.note ? ": " + q.note : "") + " ");
      rq.appendChild(tk.btn("Approve…", () => tk.send({type: "approve", request: q.id})));
      d.appendChild(rq);
    }
    r.appendChild(d);
  }
  tk.removed(r, v);
}
tk.onView(render);
