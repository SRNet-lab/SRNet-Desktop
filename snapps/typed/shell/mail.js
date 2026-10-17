"use strict";
async function api(op, body) {
  const r = await fetch("/api/mail/" + op, {method: "POST", headers: {"X-Core": "1", "Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json();
  if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
const $ = (id) => document.getElementById(id);
function el(t, txt) { const e = document.createElement(t); if (txt !== undefined) e.textContent = String(txt); return e; }
function status(s) { $("m-status").textContent = s; }
let folder = "inbox", replyTo = null;

async function refresh() {
  const s = await api("summary");
  $("m-link").value = s.link || "(set your mail hosts first)";
  $("m-hosts").value = s.hosts.join("\n");
  $("u-inbox").textContent = s.unread.inbox ? "(" + s.unread.inbox + ")" : "";
  $("u-requests").textContent = s.unread.requests ? "(" + s.unread.requests + ")" : "";
  const ul = $("m-contacts"); ul.textContent = "";
  for (const c of s.contacts) ul.appendChild(el("li", c.name));
  await showFolder(folder);
}
async function showFolder(f) {
  folder = f; $("m-view").hidden = true; $("m-compose").hidden = true;
  const list = $("m-list"); list.hidden = false; list.textContent = "";
  list.appendChild(el("h3", f === "inbox" ? "Inbox" : "Requests (people you haven't accepted)"));
  const r = await api("folder", {folder: f});
  if (!r.msgs.length) list.appendChild(el("p", "Nothing here."));
  for (const m of r.msgs) {
    const row = el("div"); row.className = "m-row" + (m.read ? "" : " unread");
    row.appendChild(el("strong", m.from_name)); row.appendChild(el("span", " · " + (m.subject || "(no subject)")));
    row.appendChild(el("span", " · " + new Date(m.ts * 1000).toLocaleString()));
    row.addEventListener("click", () => openMsg(m.msg_id));
    list.appendChild(row);
  }
}
async function openMsg(id) {
  const m = await api("open", {id});
  const v = $("m-view"); v.textContent = ""; v.hidden = false; $("m-list").hidden = true;
  v.appendChild(el("h3", m.subject || "(no subject)"));
  v.appendChild(el("div", "From " + m.from_name + (m.known ? "" : " (not in your contacts)") + " · signature verified"));
  v.appendChild(el("div", "Key " + m.from));
  v.appendChild(el("pre", m.body));
  for (const a of m.attach) v.appendChild(el("div", "Attachment (open it from SNApps > Open a link): " + a));
  const bar = el("div");
  const b = (t, fn) => { const x = el("button", t); x.addEventListener("click", fn); bar.appendChild(x); };
  b("Reply", () => { replyTo = id; compose("(reply to " + m.from_name + ")", "Re: " + m.subject); });
  if (m.folder === "requests") b("Accept sender", async () => { await api("accept", {id}); status("accepted"); refresh(); });
  b("Delete", async () => { await api("delete", {id}); refresh(); });
  b("Back", () => showFolder(folder));
  v.appendChild(bar);
  refresh_counts();
}
async function refresh_counts() { const s = await api("summary"); $("u-inbox").textContent = s.unread.inbox ? "(" + s.unread.inbox + ")" : ""; $("u-requests").textContent = s.unread.requests ? "(" + s.unread.requests + ")" : ""; }
function compose(to, subj) {
  $("m-list").hidden = true; $("m-view").hidden = true; $("m-compose").hidden = false;
  $("c-to").value = to || ""; $("c-to").disabled = !!replyTo; $("c-subject").value = subj || ""; $("c-body").value = ""; $("c-file").value = ""; $("c-attach").value = "";
}
function b64(buf) { let s = ""; const u = new Uint8Array(buf); for (let i = 0; i < u.length; i += 32768) s += String.fromCharCode.apply(null, u.subarray(i, i + 32768)); return btoa(s); }
$("c-send").addEventListener("click", async () => {
  const attach = $("c-attach").value.split(/\s+/).filter(Boolean);
  try {
    for (const f of $("c-file").files) {
      status("encrypting " + f.name + "…");
      const r = await api("attach", {data: b64(await f.arrayBuffer()), name: f.name, mime: f.type || "application/octet-stream"});
      attach.push(r.link);
    }
    const r = replyTo ? await api("reply", {id: replyTo, body: $("c-body").value, attach})
                      : await api("send", {to: $("c-to").value.trim(), subject: $("c-subject").value, body: $("c-body").value, attach});
    status(r.ok ? "sent to " + Object.keys(r.hosts).length + " host(s)" : "not delivered: " + JSON.stringify(r.hosts));
    replyTo = null; showFolder(folder);
  } catch (e) { status(e.message); }
});
$("c-cancel").addEventListener("click", () => { replyTo = null; showFolder(folder); });
$("m-compose-btn").addEventListener("click", () => { replyTo = null; compose(); });
for (const b of document.querySelectorAll(".m-folder")) b.addEventListener("click", () => showFolder(b.dataset.f));
$("m-fetch").addEventListener("click", async () => {
  try { const r = await api("fetch"); status(r.new + " new" + (r.rejected.length ? ", " + r.rejected.length + " rejected (bad signature or not for you)" : "") + (Object.keys(r.errors).length ? ", some hosts unreachable" : "")); refresh(); }
  catch (e) { status(e.message); }
});
$("m-hosts-save").addEventListener("click", async () => {
  try { await api("hosts", {hosts: $("m-hosts").value.split(/\s+/).filter(Boolean)}); status("hosts saved"); refresh(); } catch (e) { status(e.message); }
});
$("m-add").addEventListener("click", async () => {
  try { await api("contact", {link: $("m-add-link").value.trim(), name: $("m-add-name").value.trim() || undefined}); $("m-add-link").value = ""; refresh(); } catch (e) { status(e.message); }
});
refresh().catch((e) => status(e.message));
