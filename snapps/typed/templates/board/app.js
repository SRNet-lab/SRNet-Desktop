"use strict";
let replyTo = null;
function compose(r) {
  const box = tk.el("div", "item"); const ta = tk.el("textarea"); ta.maxLength = 500; ta.rows = 3;
  ta.placeholder = replyTo ? "Reply…" : "Write a post (500 max)";
  box.appendChild(ta);
  box.appendChild(tk.btn(replyTo ? "Reply" : "Post", () => { tk.draft("board.post", {text: ta.value, reply_to: replyTo}, replyTo ? [replyTo] : []); ta.value = ""; replyTo = null; }));
  if (replyTo) box.appendChild(tk.btn("cancel reply", () => { replyTo = null; render(tk.view); }));
  r.appendChild(box);
}
function render(v) {
  const r = tk.root(); r.appendChild(tk.el("h2", null, "Board · " + v.collection)); r.appendChild(tk.status(v)); tk.header(r, v);
  compose(r);
  for (const p of v.view.posts || []) {
    const d = tk.el("div", "item");
    const m = tk.el("div", "muted"); m.appendChild(tk.el("span", "who", p.mine ? "you" : p.who)); m.appendChild(document.createTextNode(" · " + tk.when(p.ts) + (p.reply_to ? " · reply" : "") + (p.replies ? " · " + p.replies + " replies" : ""))); d.appendChild(m);
    d.appendChild(tk.el("div", "body", p.text));
    d.appendChild(tk.btn("Reply", () => { replyTo = p.id; render(v); }));
    if (p.mine) d.appendChild(tk.btn("Delete", () => tk.send({type: "delete", id: p.id})));
    else { d.appendChild(tk.btn(p.followed ? "Unfollow" : "Follow", () => tk.act(p.followed ? "unfollow" : "follow", p.author))); d.appendChild(tk.btn("Mute", () => tk.act("mute", p.author))); }
    r.appendChild(d);
  }
  tk.removed(r, v);
}
tk.onView(render);
