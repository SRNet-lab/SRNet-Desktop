// Trusted shell.  Talks to the core API (X-Core header) and relays verified views into the
// sandboxed template frame.  Messages from the frame are untrusted requests: only "ready",
// "draft", "action", "delete", "pay", "download", the file messages ("open", "link", "request",
// "access", "approve", "newver"), "join" (a directory Join button: only opens the join sheet) and (test mode) "report" are understood. Keys and links stay here and in the core; and
// drafts are shown in this trusted sheet before the core signs anything.
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
function sheet(title, sub, lines, buttons, pw, input) {
  $("sheet-pwl").hidden = !pw; $("sheet-inl").hidden = !input; $("sheet-in").value = ""; $("sheet-inlt").textContent = input || ""; $("sheet-pw").value = "";
  $("sheet-title").textContent = title; $("sheet-sub").textContent = sub || "";
  $("sheet-body").textContent = lines.join("\n");
  const b = $("sheet-buttons"); b.replaceChildren();
  buttons.forEach(([label, fn]) => { const x = el("button", label); x.addEventListener("click", async () => { $("sheet").hidden = true; await fn(); }); b.appendChild(x); });
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
function show(which) { // "frame" | "home" | "wallet" | "exits"
  $("tpl").hidden = which !== "frame"; $("home").hidden = which !== "home"; $("wallet").hidden = which !== "wallet"; $("exits").hidden = which !== "exits";
  if (which !== "frame") { cur = null; clearInterval(timer); $("upload").hidden = true; }
}
function ribbon(b) { document.querySelectorAll("#ribbon button").forEach((x) => x.classList.toggle("on", x.dataset.b === b)); }
function open(c) {
  if (c.root_mismatch) return mismatch(c);
  cur = c; show("frame"); ribbon(c.kind === "exit" ? "exits" : "");
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
    if (m.type === "pay") { const r = await api("pay", {c: cur.name, listing: String(m.listing)});
      if (!r.ok) { $("status").textContent = "payment refused: " + r.error; return; }
      const s = r.sheet;
      if (r.srnet) return sheet(s.title, s.note, s.lines, [["Pay", async () => { const pr = await api("pay/confirm", {token: r.token, passphrase: $("sheet-pw").value}); $("sheet-pw").value = "";
        $("status").textContent = pr.ok ? "paid " + (pr.amount / 1e6).toFixed(2) + " USDC (lease " + pr.lease_id + ")" : "payment failed: " + pr.error; push(); }], ["Cancel", async () => {}]], true);
      return sheet("Payment details (" + s.network + ")", s.note, ["item: " + s.item, "seller: " + s.seller, "amount: " + s.amount + " " + s.currency, "to: " + s.to, "chain id: " + s.chain_id], [["Close", async () => {}]]); }
    if (m.type === "download" && /^[0-9a-f]{64}$/.test(m.sha256)) {
      return sheet("Download file?", "verified by hash before saving", ["sha256: " + m.sha256], [["Download", async () => {
        const r = await fetch("/api/blob/" + encodeURIComponent(cur.name) + "/" + m.sha256, {headers: {"X-Core": "1"}});
        if (!r.ok) { $("status").textContent = "blob unavailable"; return; }
        const url = URL.createObjectURL(await r.blob()); const a = el("a"); a.href = url; a.download = m.sha256.slice(0, 12) + ".bin"; a.click(); URL.revokeObjectURL(url); }], ["Cancel", async () => {}]]); }
    if (m.type === "verify") { const r = await api("pay/verify", {c: cur.name, order: String(m.order || "")}); $("status").textContent = r.ok ? (r.paid ? "order paid on-chain" : "NOT paid on-chain") : r.error; return; }
    if (m.type === "join") return joinFlow(String(m.link || "").slice(0, 400));
    if (["open", "link", "request", "approve", "newver", "access"].includes(m.type)) return onFile(m);
    if (m.type === "report" && S.test) { $("report").hidden = false; $("report").textContent += JSON.stringify(m.data) + "\n"; return; }
  } catch (e) { $("status").textContent = "error: " + e; }
});
document.addEventListener("DOMContentLoaded", async () => {
  frame = $("tpl"); S = await api("collections");
  document.querySelectorAll("#ribbon button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.b === "home") { show("home"); ribbon("home"); }
    else if (b.dataset.b === "wallet") { cur = null; show("wallet"); ribbon("wallet"); W.status(); }
    else { cur = null; show("exits"); ribbon("exits"); W.board(); W.status(); } }));
  W.wire();
  $("preflight-go").addEventListener("click", async () => {
    $("preflight-sum").textContent = "checking… (up to a minute)"; $("preflight-go").disabled = true;
    try { const r = await api("wallet/preflight", {});
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
  if (first) open(first); else if (tab === "exits" || tab === "wallet") { side(); $("ribbon").querySelector('[data-b="' + tab + '"]').click(); } else { show("home"); ribbon("home"); side(); }
  if (qs.get("join")) { $("joinlink").value = qs.get("join").slice(0, 400); joinFlow(qs.get("join")); } // a link handler: shows the sheet, never auto-joins
});

// ---- Exits + Wallet tabs: native views over the wallet backend (/api/wallet/* bridge in the core).
// Mainnet rules (final payment, passphrase per action, relayer allow-list) are enforced in the backend.
const W = (() => {
  const call = (name, body) => api("wallet/" + name, body || {});
  const log = (m) => { const t = new Date().toLocaleTimeString(); $("x-log").textContent = t + "  " + m + "\n" + $("x-log").textContent; };
  const fmt = (v, d) => (v === undefined || v === null) ? "-" : Number(v).toFixed(d);
  const ago = (s) => s < 3600 ? Math.floor(s / 60) + "m ago" : Math.floor(s / 3600) + "h" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m ago";
  function withPass(title, sub, lines, label, fn) { // passphrase only in this trusted sheet, never stored
    sheet(title, sub, lines, [[label, async () => fn($("sheet-pw").value)], ["Cancel", async () => {}]], true);
  }
  function fill(r) {
    $("w-addr").textContent = r.address || "no keystore (run srnet key new)";
    const unlocked = !!(r.unlocked || r.usdc_wallet !== undefined || r.deposited !== undefined);
    if (unlocked) {
      $("w-usdc").textContent = fmt(r.usdc_wallet, 2); $("w-dep").textContent = fmt(r.deposited, 2);
      if ($("w-unlock")) $("w-unlock").hidden = true;
      if ($("w-balhint")) $("w-balhint").textContent = "Balances loaded. Your passphrase isn't saved.";
    } else {
      $("w-usdc").textContent = "Unlock to see balances"; $("w-dep").textContent = "Unlock to see balances";
      if ($("w-unlock")) $("w-unlock").hidden = false;
      if ($("w-balhint")) $("w-balhint").textContent = "Press Unlock and enter your passphrase to load balances. It isn't saved.";
    }
    $("x-seller").textContent = r.seller || "-";
    $("x-conn").textContent = r.connected ? "yes (" + String(r.connected).slice(0, 12) + "…)" : "no";
    $("x-ar").checked = !!r.auto_renew;
    $("x-left").textContent = r.time_left_s !== undefined ? Math.floor(r.time_left_s / 60) + " min" + (r.seller_stopped ? " (seller stopped)" : "") : (r.seller ? "press Refresh and enter your passphrase" : "-");
    if (r.client_sesh && !$("x-cs").value) $("x-cs").value = r.client_sesh;
    if (r.relayer && !$("w-relayer").value) $("w-relayer").value = r.relayer;
    syncRelayerUi($("w-relayer").value, r.relayer || undefined);
  }
  async function status(pw) { const r = await call("status", pw ? {passphrase: pw} : {}); if (!r.ok) log("status: " + r.err); fill(r); return r; }
  async function act(name, extra, pw) {
    log(name + "…");
    const r = await call(name, Object.assign({passphrase: pw || null, client_sesh: $("x-cs").value.trim()}, extra));
    log((r.ok ? "ok " : "failed ") + (r.err || "") + (r.out ? "\n" + r.out : "")); await status(pw); return r;
  }
  // One deadline for every board load (first visit and Refresh): the fetch is aborted and the
  // message shown at 20s even if the abort or the body read never settles.
  const BOARD_DEADLINE_MS = 20000;
  const BOARD_TIMEOUT_MSG = "Couldn't load the exit board (timed out). Try Refresh.";
  let boardGen = 0;
  function boardErr(err) {
    const m = String(err || "unknown error");
    if (/timed? ?out|timeout|AbortError/i.test(m)) return BOARD_TIMEOUT_MSG;
    if (/wallet not running/i.test(m)) return m;
    return "Couldn't load the exit board (" + m.replace(/^wallet backend:\s*/i, "") + "). Try Refresh.";
  }
  async function board() {
    const gen = ++boardGen;
    $("x-boardinfo").textContent = "Loading the signed exit board…";
    const tb = $("x-board").querySelector("tbody"); tb.replaceChildren();
    const ac = typeof AbortController !== "undefined" ? new AbortController() : null;
    let timer = null;
    const deadline = new Promise((_, rej) => { timer = setTimeout(() => {
      if (ac) ac.abort(); rej(Object.assign(new Error("timed out"), {name: "AbortError"})); }, BOARD_DEADLINE_MS); });
    let r;
    try {
      const opt = {method: "POST", headers: {"X-Core": "1", "Content-Type": "application/json"}, body: "{}"};
      if (ac) opt.signal = ac.signal;
      r = await Promise.race([fetch("/api/wallet/board", opt).then((resp) => resp.json()), deadline]);
    } catch (e) {
      clearTimeout(timer);
      if (gen !== boardGen) return;
      $("x-boardinfo").textContent = boardErr(e && e.name === "AbortError" ? "timed out" : (e && e.message ? e.message : e));
      return;
    }
    clearTimeout(timer);
    if (gen !== boardGen) return;
    if (!r.ok) { $("x-boardinfo").textContent = boardErr(r.err); return; }
    $("x-boardinfo").textContent = "signed board seq " + r.seq + " from " + r.source + (r.dropped ? ", " + r.dropped + " dropped" : "") + (r.hidden ? ", " + r.hidden + " hidden (other payment contract)" : "") + (r.listings.length ? "" : " · no sellers listed right now");
    for (const l of r.listings) {
      const tr = el("tr"); [l.seller, l.region || "-", l.price, ago(l.age_s)].forEach((t) => tr.appendChild(el("td", t)));
      const td = el("td"), b = el("button", "Buy 1 hour"), c = el("button", "Connect");
      b.addEventListener("click", () => withPass("Buy 1 hour of exit?", "Exits are Beta / Untested. Payment is final. No refund.",
        ["seller: " + l.seller, "price: " + l.price + " USDC/hr", "paid from your SRNet balance"],
        "Buy (final)", (pw) => act("buy", {seller: l.seller}, pw)));
      c.addEventListener("click", () => withPass("Connect to this exit?", "uses your current lease", ["seller: " + l.seller], "Connect", (pw) => act("connect", {seller: l.seller}, pw)));
      td.append(b, c); tr.appendChild(td); tb.appendChild(tr);
    }
  }
  let topupTimer = null;
  async function pollTopup() {
    const r = await call("topup/status");
    setText("w-topupstate", r.state + (r.out ? ": " + r.out.trim().split("\n").slice(-2).join(" ") : ""));
    if (r.state !== "waiting" && topupTimer) { clearInterval(topupTimer); topupTimer = null; status(); }
  }
  function setText(id, text) {
    const n = $(id); if (!n) return;
    n.textContent = text || "";
    if (id === "w-warn" || id === "w-uri" || id === "w-topupstate") n.hidden = !text;
  }
  let savedRelayer = "";
  function syncRelayerUi(url, saved) {
    const typed = (url || "").trim();
    if (saved !== undefined) savedRelayer = (saved || "").trim();
    const ready = !!(savedRelayer && typed === savedRelayer);
    const tip = $("w-relayerhint");
    if (tip) {
      tip.hidden = ready;
      tip.textContent = ready ? "" :
        (typed && typed !== savedRelayer
          ? "Press Save to use this relayer for deposits and payments. Clear the field and Save to go back to the SRNet demo relayer."
          : "No relayer is set (it is off). Clear the field and press Save to use the SRNet demo relayer, or paste another relayer URL and Save.");
    }
    $("w-topup").disabled = !ready;
    $("w-topup").title = ready ? "Deposit after you send USDC" : "Save a relayer URL first";
    $("w-showqr").title = ready ? "Show deposit QR" : "You can preview the QR, but deposit needs a saved relayer";
  }
  async function showQr() {
    const r = await call("topup/info", {amount: $("w-amt").value});
    if (!r.ok) { setText("w-warn", "top up: " + r.err); return; }
    const need = r.relayer ? "" : "\nThe relayer is off: clear the Relayer field and Save (SRNet demo relayer) before depositing.";
    setText("w-warn", r.warning + "\nDeposit fee: " + r.fee_pct + "% of the whole deposit. Relayer fee: " + Number(r.relay_fee).toFixed(2) + " USDC." + need);
    let uri = r.uri;
    const q = r.qr ? await fetch("/api/wallet/qr.svg", {headers: {"X-Core": "1"}}) : null;
    if (q && q.ok) {
      $("w-qr").src = URL.createObjectURL(await q.blob()); // reviewed: blob: of the core's own QR svg; img-src allows blob: only
      $("w-qr").hidden = false; }
    else { $("w-qr").hidden = true; uri += "  (install the qrcode package for a QR image; the address works too)"; }
    setText("w-uri", uri);
    if (!$("w-relayer").value) $("w-relayer").value = r.relayer || "";
    const u = $("w-relayer").value || r.relayer || "";
    syncRelayerUi(u, r.relayer || savedRelayer || undefined);
  }
  function wire() {
    if (!S.wallet) { $("x-boardinfo").textContent = "wallet not running: start SRNet with `srnet ui`"; }
    syncRelayerUi($("w-relayer").value);
    $("w-relayer").addEventListener("input", () => syncRelayerUi($("w-relayer").value));
    $("x-refresh").addEventListener("click", () => { board(); withPass("Refresh lease and balances?", "your passphrase unlocks the keystore for this read only", [], "Refresh", (pw) => status(pw)); });
    const unlockBal = () => withPass("Unlock balances?", "your passphrase unlocks the keystore for this read only; it isn't saved", [], "Unlock", (pw) => status(pw));
    $("w-unlock").addEventListener("click", unlockBal);
    $("w-refresh").addEventListener("click", () => withPass("Refresh balances?", "your passphrase unlocks the keystore for this read only; it isn't saved", [], "Refresh", (pw) => status(pw)));
    $("x-disconnect").addEventListener("click", () => act("disconnect", {}));
    $("x-ar").addEventListener("change", (e) => { const on = e.target.checked;
      if (!on) return act("autorenew", {on: false});
      e.target.checked = false; withPass("Turn on auto-renew?", "buys the next hour near the end of each lease; each hour is final", [], "Turn on", (pw) => act("autorenew", {on: true}, pw)); });
    $("w-showqr").addEventListener("click", showQr);
    $("w-saverelayer").addEventListener("click", async () => {
      const r = await call("relayer", {url: $("w-relayer").value.trim()});
      setText("w-topupstate", r.ok ? "relayer saved" : (r.err || "could not save relayer"));
      if (r.ok) { $("w-relayer").value = r.relayer || ""; syncRelayerUi(r.relayer || "", r.relayer || ""); }
    });
    $("w-topup").addEventListener("click", () => {
      if ($("w-topup").disabled || !$("w-relayer").value.trim()) { setText("w-topupstate", "set and save a relayer URL first"); return; }
      withPass("Deposit USDC into SRNet balance?", "waits for the USDC you sent, then a gas-free approval and the relayer deposit — no ETH needed", ["amount: " + $("w-amt").value + " USDC"], "Deposit now", async (pw) => {
      const r = await call("topup/start", {amount: $("w-amt").value, passphrase: pw});
      if (!r.ok) { setText("w-topupstate", "top up: " + r.err); return; }
      if (!topupTimer) topupTimer = setInterval(pollTopup, 5000); pollTopup(); });
    });
  }
  return {board, status, wire};
})();
