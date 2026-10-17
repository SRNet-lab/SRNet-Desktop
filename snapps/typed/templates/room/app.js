"use strict";
let draftText = "", nickText = "", first = true;
function render(v) {
  const r = tk.root(); const w = v.view;
  r.appendChild(tk.el("h2", null, "#" + v.collection));
  tk.header(r, v);
  r.appendChild(tk.el("div", "muted", w.topic ? "Topic: " + w.topic.text + " (set by " + w.topic.by + ")" : "No topic"));
  const log = tk.el("div");
  const rm = (v.removed || []).slice().sort((a, b) => a.seq - b.seq);
  for (const l of w.lines || []) {
    while (rm.length && rm[0].seq < l.seq) log.appendChild(tk.removedEl(rm.shift()));
    const d = tk.el("div", "item"); d.appendChild(tk.el("span", "who", l.who)); d.appendChild(tk.el("span", "muted", " " + tk.when(l.ts) + " "));
    d.appendChild(tk.el("span", "body", l.text)); log.appendChild(d);
  }
  for (const x of rm) log.appendChild(tk.removedEl(x));
  r.appendChild(log);
  const ta = tk.el("input"); ta.maxLength = 500; ta.placeholder = "Say something (signed by you)"; ta.value = draftText;
  ta.addEventListener("input", () => { draftText = ta.value; });
  ta.dataset.k = "msg";
  const send = () => { if (!ta.value.trim()) return; tk.draft("room.msg", {text: ta.value.trim()}); draftText = ""; ta.value = ""; };
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
  r.appendChild(ta); r.appendChild(tk.btn("Post", send));
  const nick = tk.el("input"); nick.maxLength = 24; nick.placeholder = "nick"; nick.dataset.k = "nick"; nick.value = nickText;
  nick.addEventListener("input", () => { nickText = nick.value; });
  const setNick = () => { if (!nick.value.trim()) return; tk.draft("room.join", {nick: nick.value.trim()}); nickText = ""; nick.value = ""; };
  nick.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); setNick(); } });
  r.appendChild(nick); r.appendChild(tk.btn("Set nick", setNick));
  if (first) { ta.focus(); first = false; }
}
tk.onView(render);
