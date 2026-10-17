"use strict";
const TOKEN = document.querySelector('meta[name="csrf"]').content;
const $ = (id) => document.getElementById(id);

function log(msg) {
  const t = new Date().toLocaleTimeString();
  $("log").textContent = t + "  " + msg + "\n" + $("log").textContent;
}

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": TOKEN},
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

function ago(s) {
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m ago";
}

function cell(tr, text) {
  const td = document.createElement("td");
  td.textContent = text;
  tr.appendChild(td);
  return td;
}

async function loadBoard() {
  const r = await api("/api/board");
  const tb = $("board").querySelector("tbody");
  tb.textContent = "";
  if (!r.ok) { $("boardinfo").textContent = "board unavailable: " + r.err; return; }
  $("boardinfo").textContent = "board seq " + r.seq + " from " + r.source +
    (r.dropped ? ", " + r.dropped + " dropped" : "") + (r.hidden ? ", " + r.hidden + " hidden (other payment contract)" : "");
  for (const l of r.listings) {
    const tr = document.createElement("tr");
    cell(tr, l.seller); cell(tr, l.region || "-"); cell(tr, l.price); cell(tr, ago(l.age_s));
    const td = cell(tr, "");
    const b = document.createElement("button");
    b.textContent = "Buy 1 hour";
    b.onclick = () => buy(l.seller);
    const c = document.createElement("button");
    c.textContent = "Connect";
    c.onclick = () => act("/api/connect", {seller: l.seller});
    td.appendChild(b); td.appendChild(c);
    tb.appendChild(tr);
  }
}

async function loadStatus() {
  const r = await api("/api/status", {passphrase: $("pw").value});
  if (!r.ok) { log("status: " + r.err); }
  const f = (v, d) => (v === undefined || v === null) ? "-" : Number(v).toFixed(d);
  $("addr").textContent = r.address || "no keystore (run srnet key new)";
  if (r.unlocked || r.usdc_wallet !== undefined || r.deposited !== undefined) {
    $("usdc").textContent = f(r.usdc_wallet, 2);
    $("dep").textContent = f(r.deposited, 2);
  } else {
    $("usdc").textContent = "Unlock to see balances";
    $("dep").textContent = "Unlock to see balances";
  }
  $("seller").textContent = r.seller || "-";
  $("conn").textContent = r.connected ? "yes (" + r.connected.slice(0, 12) + "...)" : "no";
  $("ar").checked = !!r.auto_renew;
  if (r.time_left_s !== undefined) {
    const s = r.time_left_s;
    $("left").textContent = Math.floor(s / 60) + " min" + (r.seller_stopped ? " (seller stopped)" : "");
  } else {
    $("left").textContent = r.seller ? "enter passphrase and refresh" : "-";
  }
  if (r.client_sesh && !$("cs").value) $("cs").value = r.client_sesh;
}

async function act(path, extra) {
  const body = Object.assign({passphrase: $("pw").value, client_sesh: $("cs").value.trim()}, extra);
  log(path.replace("/api/", "") + "...");
  const r = await api(path, body);
  log((r.ok ? "ok " : "failed ") + (r.err || "") + (r.out ? "\n" + r.out : ""));
  await loadStatus();
  return r;
}

function buy(seller) {
  if (!confirm("Buy 1 hour from " + seller.slice(0, 16) + "...? Paid from your SRNet balance. Exits are Beta / Untested. Payment is final.")) return;
  act("/api/buy", {seller: seller});
}

function showTab(name) {
  for (const p of document.querySelectorAll(".pane")) p.hidden = p.id !== "tab-" + name;
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("on", b.dataset.tab === name);
}
for (const b of document.querySelectorAll(".tab")) b.onclick = () => showTab(b.dataset.tab);

async function loadSnapps() {
  const r = await api("/api/snapps");
  if (r.url && /^http:\/\/127\.0\.0\.1:\d+\/$/.test(r.url)) { $("snappslink").href = r.url; $("snappslink").textContent = r.url; }
}

async function showQr() {
  const r = await api("/api/topup/info", {amount: $("amt").value});
  if (!r.ok) { log("top up: " + r.err); return; }
  $("warn").textContent = r.warning + "\nDeposit fee: " + r.fee_pct + "% of the whole deposit. Relayer fee: " + r.relay_fee.toFixed(2) + " USDC.";
  $("uri").textContent = r.uri;
  if (r.qr) { $("qr").src = "/qr.svg?k=" + encodeURIComponent(TOKEN) + "&t=" + Date.now(); $("qr").hidden = false; }
  else { $("qr").hidden = true; log("install the qrcode package to show a QR image; the address above works too"); }
  if (!$("relayer").value) $("relayer").value = r.relayer || "";
}

let topupTimer = null;
async function pollTopup() {
  const r = await api("/api/topup/status");
  $("topupstate").textContent = r.state + (r.out ? ": " + r.out.trim().split("\n").slice(-2).join(" ") : "");
  if (r.state !== "waiting" && topupTimer) { clearInterval(topupTimer); topupTimer = null; loadStatus(); }
}

$("showqr").onclick = showQr;
$("saverelayer").onclick = async () => { const r = await api("/api/relayer", {url: $("relayer").value.trim()}); log(r.ok ? "relayer saved" : r.err); };
$("topup").onclick = async () => {
  const r = await api("/api/topup/start", {amount: $("amt").value, passphrase: $("pw").value});
  if (!r.ok) { log("top up: " + r.err); return; }
  log("waiting for USDC to arrive...");
  if (!topupTimer) topupTimer = setInterval(pollTopup, 5000);
  pollTopup();
};
showTab("exits");
loadSnapps();

$("refresh").onclick = () => { loadBoard(); loadStatus(); };
$("disconnect").onclick = () => act("/api/disconnect", {});
$("ar").onchange = (e) => act("/api/autorenew", {on: e.target.checked});
loadBoard();
loadStatus();
setInterval(loadStatus, 60000);
