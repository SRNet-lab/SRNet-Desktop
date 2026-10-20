// Trusted shell.  Talks to the core API (X-Core header) and relays verified views into the
// sandboxed template frame.  Messages from the frame are untrusted requests: only "ready",
// "draft", "action", "delete", "pay", "download", the file messages ("open", "link", "request",
// "access", "approve", "newver"), "join" (a directory Join button: only opens the join sheet), "verify" and (test mode) "report" are understood. Keys and links stay here and in the core; and
// drafts are shown in this trusted sheet before the core signs anything. Add-on tabs (e.g. Exits) are
// scripts served by the core from this origin (/addon/<name>/tab.js); they call /api/addon/<name>/*.
"use strict";
let S = null, cur = null, frame = null, timer = null;
const $ = (id) => document.getElementById(id);
async function api(path, body, raw) {
  const opt = {headers: {"X-Core": "1"}};
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = raw ? "application/octet-stream" : "application/json"; opt.body = raw ? body : JSON.stringify(body); }
  const r = await fetch("/api/" + path, opt);
  return r.json();
}
function el(t, txt) { const e = document.createElement(t); if (txt !== undefined) e.textContent = String(txt); return e; }
function sheet(title, sub, lines, buttons, img, input) {
  $("sheet-inl").hidden = !input; $("sheet-in").value = ""; $("sheet-inlt").textContent = input || "";
  const im = $("sheet-img"); im.hidden = !img; im.removeAttribute("src");
  if (img) im.src = img; // reviewed: img is only ever a blob: URL of the core's own QR svg (img-src allows blob: only)
  $("sheet-title").textContent = title; $("sheet-sub").textContent = sub || "";
  $("sheet-body").textContent = lines.join("\n");
  const b = $("sheet-buttons"); b.replaceChildren();
  buttons.forEach(([label, fn, keep]) => { const x = el("button", label); x.addEventListener("click", async () => { if (!keep) $("sheet").hidden = true; await fn(x); }); b.appendChild(x); });
  $("sheet").hidden = false;
}
async function saveFile(path, body) {
  const r = await fetch("/api/" + path, {method: "POST", headers: {"X-Core": "1", "Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!r.ok) { const e = await r.json().catch(() => ({})); $("status").textContent = "can't open: " + (e.error || r.status); return; }
  const cd = r.headers.get("Content-Disposition") || ""; const m = /filename="([^"]+)"/.exec(cd);
  const url = URL.createObjectURL(await r.blob()); const a = el("a"); a.href = url; a.download = m ? m[1] : "file"; a.click(); URL.revokeObjectURL(url);
}
function draftSheet(d, title) {
  if (d.error) { $("status").textContent = "refused: " + d.error; return; }
  if (d.result) { push(); return; }
  sheet(title || "Sign and post?", d.collection + " · " + d.t, d.lines, [["Sign & post", async () => { const r = await api("confirm", {token: d.token}); $("status").textContent = r.ok ? "posted" : (r.queued ? "queued in outbox" : "refused: " + r.error); push(); }], ["Cancel", async () => api("cancel", {token: d.token})]]);
}
async function onFile(m) {
  const id = String(m.id || ""), c = cur.name;
  if (m.type === "open") return sheet("Open file?", "fetched by hash, decrypted on this computer", ["file: " + id], [["Open", async () => saveFile("file/open", {c, id})], ["Cancel", async () => {}]]);
  if (m.type === "link") { const r = await api("file/link", {c, id}); if (r.error) { $("status").textContent = r.error; return; }
    return sheet("Public link", r.note, [r.link], [["Copy", async () => { try { await navigator.clipboard.writeText(r.link); } catch (e) {} }], ["Close", async () => {}]]); }
  if (m.type === "request") { const note = prompt("Note to the owner (optional)") || ""; return draftSheet(await api("file/request", {c, id, note}), "Ask the owner for access?"); }
  if (m.type === "approve") return draftSheet(await api("file/approve", {c, request: String(m.request || "")}), "Share this file with the requester?");
  if (m.type === "newver") { $("upload").dataset.prev = id; $("status").textContent = "pick the new file and press upload; revoked people won't get it"; return; }
  if (m.type === "access") { const r = await api("file/access", {c, id}); if (r.error) { $("status").textContent = r.error; return; }
    const lines = ["mode: " + r.mode, "shared with: " + (r.recipients.length ? "" : "nobody else")].concat(r.recipients.map((k) => "  " + k), r.revoked.length ? ["revoked: " + r.revoked.length] : [], [r.note]);
    const btns = [["Share with…", async () => { const to = (prompt("Their key (profile key or encryption key)") || "").trim(); if (to) draftSheet(await api("file/grant", {c, id, to}), "Share this file?"); }]];
    r.recipients.forEach((k) => btns.push(["Revoke " + k.slice(0, 8) + "…", async () => draftSheet(await api("file/revoke", {c, id, to: k}), "Revoke for future versions?")]));
    btns.push(["Close", async () => {}]);
    return sheet("Who can open this file", "", lines, btns); }
}
function post(msg) { if (frame && frame.contentWindow) frame.contentWindow.postMessage(msg, "*"); }
async function push() { if (!cur) return; const v = await api("view/" + encodeURIComponent(cur.name) + "?read=1"); post({type: "view", data: v}); if (!SIDE.loaded) side();
  const st = v.status || {}; $("status").textContent = cur.name + " · " + (st.error ? "STALE: " + st.error : "verified via " + (st.host || "-")) + (v.hidden ? " · " + v.hidden + " hidden by host" : ""); }
function show(which) { // "frame" | "home" | "addon:<name>"
  $("tpl").hidden = which !== "frame"; $("home").hidden = which !== "home";
  document.querySelectorAll("section.addon").forEach((x) => { const on = which === "addon:" + x.dataset.addon; if (x.hidden === on) { x.hidden = !on; const a = (window.SRNetAddons || {})[x.dataset.addon]; if (a) (on ? a.show : (a.hide || (() => {})))(); } });
  if (which !== "frame") { cur = null; clearInterval(timer); $("upload").hidden = true; }
}
// Add-on tabs: a ribbon button and a section each; the tab script registers window.SRNetAddons[name].
const SHELL_API = {api, el, sheet: (t, s, l, b) => sheet(t, s, l, b), status: (m) => { $("status").textContent = m; }};
function addonTabs(list) {
  (list || []).forEach((a) => {
    if (!/^[a-z][a-z0-9-]{0,31}$/.test(a.name)) return;
    const b = el("button", a.title); b.dataset.b = "addon:" + a.name; $("ribbon").appendChild(b);
    const sec = el("section"); sec.className = "addon"; sec.dataset.addon = a.name; sec.hidden = true; $("addons").appendChild(sec);
    const css = document.createElement("link"); css.rel = "stylesheet"; css.href = "/addon/" + a.name + "/tab.css"; document.head.appendChild(css);
    const sc = document.createElement("script");
    sc.src = "/addon/" + a.name + "/tab.js"; // reviewed: same-origin path, name checked above; served by the core
    sc.addEventListener("load", () => { const t = (window.SRNetAddons || {})[a.name]; if (!t) return; t.mount(sec, SHELL_API); if (!sec.hidden) t.show(); });
    document.head.appendChild(sc);
  });
}
function ribbon(b) { document.querySelectorAll("#ribbon button").forEach((x) => x.classList.toggle("on", x.dataset.b === b)); }
function open(c) {
  if (c.root_mismatch) return mismatch(c);
  cur = c; show("frame"); ribbon("");
  document.querySelectorAll("#side .item").forEach((b) => b.classList.toggle("on", b.dataset.id === c.name));
  $("upload").hidden = c.kind !== "drop";
  frame.src = "/frame/" + encodeURIComponent(c.template) + "?c=" + encodeURIComponent(c.name);
  clearInterval(timer); timer = setInterval(push, c.kind === "room" ? 1500 : 4000);
  if (SIDE.loaded) side(); // first load: push() fetches the sidebar after the view
}
function mismatch(c) {
  const m = c.root_mismatch;
  sheet("WARNING: this collection's root key changed", (c.label || c.name) + (c.host ? " on " + c.host : ""),
    ["The host now presents a different root key than the one you trusted when you joined.",
     "Sync and posting are blocked. This can mean the host was taken over or is impersonated.",
     "Only re-trust if the owner told you, through another channel, that the key changed.", "",
     "trusted: " + m.pinned, "offered: " + m.offered],
    [["Keep blocked", async () => {}], ["Re-trust new key…", async () => retrust(c)]]);
}
function retrust(c) {
  sheet("Re-trust " + (c.label || c.name) + "?", "Your cached copy will be wiped and re-synced under the new key.", ["new root: " + c.root_mismatch.offered],
    [["Re-trust", async () => { const r = await api("retrust", {c: c.name, root: c.root_mismatch.offered, confirm: $("sheet-in").value});
      $("status").textContent = r.error ? "re-trust refused: " + r.error : "re-trusted " + c.label; side(); }], ["Cancel", async () => {}]],
    false, "Type: trust the new key");
}
async function joinFlow(link) {
  const p = await api("join/preview", {link: String(link || "")});
  if (p.error) { $("status").textContent = "can't join: " + p.error; return; }
  if (p.choices) { show("home"); ribbon("home"); const box = $("choices"); box.replaceChildren(el("div", p.host + " offers:"));
    p.choices.forEach((ch) => { const b = el("button", "Join " + ch.name + " (" + ch.kind + ")"); b.addEventListener("click", () => joinFlow(ch.link)); box.appendChild(b); });
    if (!p.choices.length) box.appendChild(el("div", "no collections listed")); return; }
  const lines = ["name: " + p.name, "type: " + p.kind, "host: " + p.host, "root key: " + p.root,
    p.pinned_by_link ? "root key matches the key in the link" : "link carried no key: you'll trust this one from now on (first use)"];
  if (p.rules) lines.push("rules: " + p.rules);
  if (p.root_mismatch) return sheet("WARNING: root key changed", p.host, lines.concat(["", "You trusted a different key before: " + p.root_mismatch.pinned, "Join blocked."]), [["Close", async () => {}]]);
  const isMail = p.kind === "mail";
  if (isMail) lines[1] = "type: mail host (your inbox gets a copy here)";
  if (p.already_joined.length && isMail) return sheet("Already your mail host", p.host, lines, [["Open Mail", async () => { location.href = "/mail"; }], ["Close", async () => {}]]);
  if (p.already_joined.length) return sheet("Already joined", p.name, lines, [["Open", async () => { await side(); const it = findItem(p.already_joined[0]); if (it) open(it); }], ["Close", async () => {}]]);
  sheet(isMail ? "Use " + p.host + " as a mail host?" : "Join " + p.name + "?", "verified: signed by the root key below", lines,
    [[isMail ? "Add mail host" : "Join", async () => { const r = await api("join", {link: p.link});
      if (!r.joined) { $("status").textContent = "join refused: " + (r.error || "?"); return; }
      if (r.mail) { $("status").textContent = "mail host added"; location.href = "/mail"; return; }
      await side(); const it = findItem(r.id); if (it) open(it); }], ["Cancel", async () => {}]]);
}
let SIDE = {groups: []};
function findItem(id) { for (const g of SIDE.groups) for (const i of g.items) if (i.name === id) return i; return null; }
async function side() {
  api("mail/summary", {}).then((m) => { const n = (m.unread || {}).inbox || 0, q = (m.unread || {}).requests || 0;
    $("side-mail-n").textContent = n || q ? "(" + n + (q ? " + " + q + " requests" : "") + ")" : ""; }).catch(() => {});
  SIDE = await api("sidebar"); SIDE.loaded = true; const box = $("groups"); box.replaceChildren();
  SIDE.groups = SIDE.groups.map((g) => ({...g, items: g.items.map((i) => ({...i, name: i.id, host: g.host}))}));
  SIDE.groups.forEach((g) => { const h = el("div", g.short); h.className = "host"; h.title = g.host; box.appendChild(h);
    g.items.forEach((i) => {
      const row = el("div"); row.className = "item" + (i.muted ? " muted" : "") + (cur && cur.name === i.name ? " on" : ""); row.dataset.id = i.name;
      const nm = el("button", (i.pinned ? "★ " : "") + i.label); nm.className = "nm"; nm.title = i.kind + " · " + i.id; nm.addEventListener("click", () => open(i)); row.appendChild(nm);
      if (i.root_mismatch) { const w = el("span", "⚠"); w.className = "warn"; w.title = "root key changed: blocked"; row.appendChild(w); }
      else if (i.unread) { const u = el("span", i.unread > 99 ? "99+" : i.unread); u.className = "badge"; row.appendChild(u); }
      const more = el("button", "⋯"); row.appendChild(more);
      const menu = el("div"); menu.className = "menu";
      more.addEventListener("click", () => menu.classList.toggle("open"));
      const act = (label, fn) => { const b = el("button", label); b.addEventListener("click", fn); menu.appendChild(b); return b; };
      act("Rename", async () => { const n = prompt("Name for " + i.name, i.label); if (n !== null) { await api("coll/update", {c: i.name, label: n}); side(); } });
      act(i.muted ? "Unmute" : "Mute", async () => { await api("coll/update", {c: i.name, muted: !i.muted}); side(); });
      act(i.pinned ? "Unpin" : "Pin", async () => { await api("coll/update", {c: i.name, pinned: !i.pinned}); side(); });
      act("Share", async () => { const r = await api("share", {c: i.name}); if (r.error) { $("status").textContent = r.error; return; }
        sheet("Invite link", i.label + " · anyone with this link can join and will verify the same root key", [r.link],
          [["Copy", async () => { try { await navigator.clipboard.writeText(r.link); $("status").textContent = "link copied"; } catch (e) { $("status").textContent = "copy failed: select the link text"; } }], ["Close", async () => {}]]); });
      act("Leave", async () => { if (!confirm("Leave " + i.label + "? Its cached copy is deleted.")) return; await api("leave", {c: i.name}); if (cur && cur.name === i.name) { show("home"); ribbon("home"); } side(); }).classList.add("danger");
      box.appendChild(row); box.appendChild(menu); }); });
  if (!SIDE.groups.some((g) => g.items.length)) { const e = el("div", "Nothing joined yet. Paste an srnet:// link on Home, or use the Join buttons in a directory. Rooms, file drops and stores show up here once you join them.");
    e.className = "muted empty"; e.id = "side-empty"; box.appendChild(e); }
}
async function onDraft(m) {
  const d = await api("draft", {c: cur.name, t: String(m.t), body: m.body || {}, refs: Array.isArray(m.refs) ? m.refs : []});
  if (d.error) { $("status").textContent = "draft refused: " + d.error; return; }
  if (d.result) { push(); return; }
  sheet("Sign and post?", d.collection + " · " + d.t + (d.refs.length ? " · refs " + d.refs.join(",") : ""), d.lines,
    [["Sign & post", async () => { const r = await api("confirm", {token: d.token}); $("status").textContent = r.ok ? "posted" : (r.queued ? "queued in outbox" : "refused: " + r.error); push(); }],
     ["Cancel", async () => api("cancel", {token: d.token})]]);
}
window.addEventListener("message", async (ev) => {
  if (!frame || ev.source !== frame.contentWindow || ev.origin !== "null") return; // only our sandboxed frame
  const m = ev.data; if (!m || typeof m !== "object") return;
  try {
    if (m.type === "ready") return push();
    if (m.type === "draft") return onDraft(m);
    if (m.type === "action") { await api("action", {name: String(m.name), args: {target: String(m.target || "")}}); return push(); }
    if (m.type === "delete") { await api("delete", {c: cur.name, id: String(m.id)}); return push(); }
    if (m.type === "pay") return onPay(String(m.listing || ""));
    if (m.type === "download" && /^[0-9a-f]{64}$/.test(m.sha256)) {
      return sheet("Download file?", "verified by hash before saving", ["sha256: " + m.sha256], [["Download", async () => {
        const r = await fetch("/api/blob/" + encodeURIComponent(cur.name) + "/" + m.sha256, {headers: {"X-Core": "1"}});
        if (!r.ok) { $("status").textContent = "blob unavailable"; return; }
        const url = URL.createObjectURL(await r.blob()); const a = el("a"); a.href = url; a.download = m.sha256.slice(0, 12) + ".bin"; a.click(); URL.revokeObjectURL(url); }], ["Cancel", async () => {}]]); }
    if (m.type === "verify") { const r = await api("pay/verify", {c: cur.name, order: String(m.order || "")});
      if (!r.ok) { $("status").textContent = r.error || r.err || "can't check"; return; }
      if (!r.checked) return sheet("Check this payment", "", [r.note || "", r.link || ""].filter(Boolean), [["Copy link", async () => { try { await navigator.clipboard.writeText(r.link || ""); } catch (e) {} }], ["Close", async () => {}]]);
      return sheet(r.paid ? "Paid" : "Not paid (yet)", r.link || "", r.paid ? ["amount: " + r.amount + " USDC", "from: " + r.from, "chain: " + r.chain, "block: " + r.block] : [r.why || "no matching payment"], [["Close", async () => {}]]); }
    if (m.type === "join") return joinFlow(String(m.link || "").slice(0, 400));
    if (["open", "link", "request", "approve", "newver", "access"].includes(m.type)) return onFile(m);
    if (m.type === "report" && S.test) { $("report").hidden = false; $("report").textContent += JSON.stringify(m.data) + "\n"; return; }
  } catch (e) { $("status").textContent = "error: " + e; }
});
document.addEventListener("DOMContentLoaded", async () => {
  frame = $("tpl"); S = await api("collections");
  addonTabs(S.addons);
  $("ribbon").addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (!b) return;
    if (b.dataset.b === "home") { show("home"); ribbon("home"); }
    else if (b.dataset.b && b.dataset.b.startsWith("addon:")) { cur = null; show(b.dataset.b); ribbon(b.dataset.b); } });
  $("preflight-go").addEventListener("click", async () => {
    $("preflight-sum").textContent = "checking… (up to a minute)"; $("preflight-go").disabled = true;
    try { const r = await api("preflight", {});
      if (!r.ok) { $("preflight-sum").textContent = "couldn't run: " + r.err; return; }
      $("preflight-sum").textContent = "result: " + r.report.preflight + " · report saved to " + r.file;
      $("preflight-out").textContent = r.text; $("preflight-out").hidden = false;
    } finally { $("preflight-go").disabled = false; } });
  $("join-go").addEventListener("click", () => joinFlow($("joinlink").value.trim()));
  $("joinlink").addEventListener("keydown", (e) => { if (e.key === "Enter") joinFlow($("joinlink").value.trim()); });
  setInterval(side, 10000);
  $("modtoggle").addEventListener("click", async () => {
    const on = $("modtoggle").dataset.on === "1";
    await api("moderation", {mode: on ? "honour" : "ignore"});
    $("modtoggle").dataset.on = on ? "0" : "1";
    $("modtoggle").textContent = on ? "Show host-hidden" : "Honor host hides";
    const tip = on
      ? "Shows posts and files the host has hidden, so you can still read them. Off by default: the client honors host hides."
      : "Currently showing host-hidden posts and files. Click to honor host hides again.";
    $("modtoggle").title = tip;
    if ($("modhint")) $("modhint").title = tip;
    push();
  });
  $("send-file").addEventListener("click", async () => { const f = $("file").files[0]; if (!f) return;
    const prev = $("upload").dataset.prev || ""; delete $("upload").dataset.prev;
    const q = "upload?c=" + encodeURIComponent(cur.name) + "&name=" + encodeURIComponent(f.name) + "&mime=" + encodeURIComponent(f.type || "application/octet-stream") + "&note=" + encodeURIComponent($("note").value) + "&mode=" + encodeURIComponent($("mode").value) + "&to=" + encodeURIComponent($("to").value) + (prev ? "&prev=" + encodeURIComponent(prev) : "");
    if ($("mode").value === "plain" && !confirm("This file will be stored unencrypted and anyone can read it. Continue?")) return;
    const d = await api(q, await f.arrayBuffer(), true);
    if (d.token) sheet("Sign and share this file?", d.collection, d.lines, [["Sign & post", async () => { await api("confirm", {token: d.token}); push(); }], ["Cancel", async () => api("cancel", {token: d.token})]]);
    else $("status").textContent = "upload failed: " + (d.error || "?"); });
  $("openlink-go").addEventListener("click", async () => { const link = $("openlink").value.trim(); if (link) await saveFile("file/openlink", {link}); });
  const qs = new URLSearchParams(location.search), want = qs.get("c");
  const first = want && S.collections.find((c) => c.name === want);
  const tab = qs.get("tab");
  const tabBtn = tab && /^[a-z][a-z0-9-]{0,31}$/.test(tab) ? $("ribbon").querySelector('[data-b="addon:' + tab + '"]') : null;
  if (first) open(first); else if (tabBtn) { side(); tabBtn.click(); } else { show("home"); ribbon("home"); side(); }
  if (qs.get("join")) { $("joinlink").value = qs.get("join").slice(0, 400); joinFlow(qs.get("join")); } // a link handler: shows the sheet, never auto-joins
});

// ---- store payments: the core shows the address, the exact amount and a QR code; the buyer pays
// from their own wallet. Nothing here sends funds. Afterwards the buyer can post the tx hash as an order.
async function onPay(listing) {
  const r = await api("pay", {c: cur.name, listing});
  if (!r.ok) { $("status").textContent = "can't pay: " + (r.error || r.err || "?"); return; }
  const s = r.sheet;
  let img = null;
  if (r.qr) { const q = await fetch("/api/pay/qr.svg?token=" + encodeURIComponent(r.token), {headers: {"X-Core": "1"}});
    if (q.ok) img = URL.createObjectURL(await q.blob()); } // blob: of the core's own QR svg; img-src allows blob: only
  const lines = ["item: " + s.item, "seller: " + s.seller, "send exactly: " + s.amount + " " + s.currency, "to: " + s.to,
    "network: " + s.network + " (chain id " + s.chain_id + ")" + (s.testnet ? " · TEST network" : "")];
  if (s.token_address) lines.push(s.currency + " contract: " + s.token_address);
  lines.push("", "payment link: " + s.uri);
  sheet("Pay " + s.amount + " " + s.currency + " on " + s.network, s.note, lines,
    [["Copy address", async (x) => { try { await navigator.clipboard.writeText(s.to); x.textContent = "Address copied"; } catch (e) { x.textContent = "Select the address above"; } }, true],
     ["Copy amount", async (x) => { try { await navigator.clipboard.writeText(s.amount); x.textContent = "Amount copied"; } catch (e) { x.textContent = "Select the amount above"; } }, true],
     ["I paid: tell the seller", async () => { const tx = $("sheet-in").value.trim();
       const d = await api("pay/order", {token: r.token, tx});
       if (d.error) { $("status").textContent = d.error; return; }
       draftSheet(d, "Post this order for the seller?"); }],
     ["Close", async () => {}]], img, "Transaction hash (after paying)");
}
