// SRNet message board template.  SECURITY RULE: post data is only ever placed in the page
// with textContent / createTextNode.  Never use innerHTML, outerHTML, insertAdjacentHTML,
// document.write, eval, or set href/src from post data.  tests/test_xss.py enforces this.
"use strict";
let state = null, view = "all", replyTo = null;

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = String(text);
  return e;
}
async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  return r.json();
}
const short = (k) => k.slice(0, 12) + "…";
const when = (ts) => new Date(ts * 1000).toLocaleString();

function postNode(p, focus) {
  if (p.gone) return el("div", "post muted", p.gone === "delete" ? "[deleted by its author]" : "[expired under the host's retention policy]");
  const d = el("div", "post" + (focus ? " focus" : ""));
  const meta = el("div", "muted");
  meta.appendChild(el("span", "author", p.mine ? "you" : short(p.author)));
  meta.appendChild(document.createTextNode(" · " + when(p.ts) + " · #" + p.seq));
  if (p.reply_to) meta.appendChild(document.createTextNode(" · reply"));
  d.appendChild(meta);
  d.appendChild(el("div", "body", p.body));
  const act = el("div");
  const b = (label, fn) => { const x = el("button", null, label); x.addEventListener("click", fn); act.appendChild(x); };
  b("Thread" + (p.replies ? " (" + p.replies + ")" : ""), () => showThread(p.id));
  b("Reply", () => { replyTo = p.id; $("replying").hidden = false; $("replying-id").textContent = p.id; $("body").focus(); });
  if (p.mine) b("Delete", async () => { await api("/api/delete", {id: p.id}); refresh(); });
  if (p.host_hidden) {
    meta.appendChild(document.createTextNode(" · hidden by host, shown by your choice"));
    b("Hide again", async () => { await api("/api/reveal", {id: p.id, on: false}); refresh(); });
  }
  if (!p.mine) {
    b(p.followed ? "Unfollow" : "Follow", async () => { await api("/api/follow", {author: p.author, on: !p.followed}); refresh(); });
    b("Mute", async () => { await api("/api/mute", {author: p.author, on: true}); refresh(); });
  }
  d.appendChild(act);
  return d;
}

async function showTimeline() {
  const r = await api("/api/timeline?view=" + encodeURIComponent(view));
  const v = $("view"); v.replaceChildren();
  if (!r.posts.length) v.appendChild(el("p", "muted", "No posts yet."));
  r.posts.forEach((p) => v.appendChild(postNode(p)));
}
async function showThread(id) {
  view = "thread";
  const r = await api("/api/thread/" + encodeURIComponent(id));
  const v = $("view"); v.replaceChildren();
  const back = el("button", null, "← back"); back.addEventListener("click", () => { view = "all"; refresh(); });
  v.appendChild(back);
  r.parents.forEach((p) => v.appendChild(postNode(p)));
  if (r.root) v.appendChild(postNode(r.root, true)); else v.appendChild(el("p", "muted", "Post unavailable."));
  r.replies.forEach((p) => v.appendChild(postNode(p)));
}
function showPeople() {
  const v = $("view"); v.replaceChildren();
  for (const [kind, label] of [["follows", "Following"], ["mutes", "Muted"]]) {
    v.appendChild(el("h3", null, label));
    if (!state[kind].length) v.appendChild(el("p", "muted", "none"));
    state[kind].forEach((k) => {
      const row = el("div", "post"); row.appendChild(el("span", "author", k));
      const x = el("button", null, "remove");
      x.addEventListener("click", async () => { await api(kind === "follows" ? "/api/follow" : "/api/mute", {author: k, on: false}); refresh(); });
      row.appendChild(x); v.appendChild(row);
    });
  }
}
async function refresh() {
  state = await api("/api/state");
  $("board").textContent = state.board;
  const l = state.last || {};
  $("status").textContent = "you " + short(state.me) + " · verified to #" + state.seq +
    (l.host ? " · via " + l.host : "") + (l.error ? " · STALE: " + l.error : "") +
    (state.outbox ? " · " + state.outbox + " waiting in outbox" : "") +
    " · host moderation: " + state.moderation + (state.hidden ? " (" + state.hidden + " hidden)" : "");
  $("modtoggle").textContent = state.moderation === "honour" ? "Show host-hidden posts" : "Honour host hides";
  $("rules").textContent = state.rules || "";
  if (view === "people") showPeople(); else if (view !== "thread") await showTimeline();
}
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("nav button[data-view]").forEach((b) => b.addEventListener("click", () => { view = b.dataset.view; refresh(); }));
  $("modtoggle").addEventListener("click", async () => { await api("/api/moderation", {mode: state.moderation === "honour" ? "ignore" : "honour"}); refresh(); });
  $("sync").addEventListener("click", async () => { await api("/api/sync", {}); refresh(); });
  $("body").addEventListener("input", () => { $("count").textContent = $("body").value.length + "/500"; });
  $("cancel-reply").addEventListener("click", () => { replyTo = null; $("replying").hidden = true; });
  $("send").addEventListener("click", async () => {
    $("send").disabled = true; $("status").textContent = "computing proof of work and signing…";
    const r = await api("/api/post", {body: $("body").value, reply_to: replyTo});
    $("send").disabled = false;
    if (r.ok) { $("body").value = ""; replyTo = null; $("replying").hidden = true; }
    else if (r.queued) { $("body").value = ""; replyTo = null; $("replying").hidden = true; }
    else { $("status").textContent = "not posted: " + (r.error || "error"); return; }
    refresh();
  });
  refresh(); setInterval(() => { if (view !== "thread" && view !== "people") refresh(); }, 5000);
});
