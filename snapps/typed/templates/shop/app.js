"use strict";
const NETS = {42161: "Arbitrum One", 8453: "Base", 421614: "Arbitrum Sepolia", 84532: "Base Sepolia", 11155111: "Sepolia"};
function render(v) {
  const r = tk.root(); r.appendChild(tk.el("h2", null, "Store · " + v.collection)); r.appendChild(tk.status(v));
  for (const l of v.view.listings || []) {
    if (!l.active) continue;
    const d = tk.el("div", "item"); d.appendChild(tk.el("h3", null, l.title));
    d.appendChild(tk.el("div", "body", l.desc || ""));
    const net = NETS[l.chain_id] || ("chain " + l.chain_id);
    d.appendChild(tk.el("div", "muted", (l.payable ? l.price + " " + l.currency + " on " + net : l.note || "seller needs to update") + " · seller " + l.seller_label + " · " + (l.rating ? "★ " + l.rating + " (" + l.reviews.length + ")" : "no reviews")));
    // The core shows its own payment sheet (address, amount, QR); this page never builds a payment.
    if (l.payable) d.appendChild(tk.btn("Pay…", () => tk.send({type: "pay", listing: l.id})));
    const rt = tk.el("input"); rt.placeholder = "rating 1-5"; const tx = tk.el("input"); tx.placeholder = "review";
    d.appendChild(rt); d.appendChild(tx);
    d.appendChild(tk.btn("Review", () => tk.draft("shop.review", {rating: parseInt(rt.value, 10), text: tx.value || null}, [l.id])));
    for (const rv of l.reviews) d.appendChild(tk.el("div", "muted", "★" + rv.rating + " " + rv.by + ": " + rv.text));
    if (l.seller_label && v.me && l.seller && l.seller.slice(0, 6) === v.me) for (const o of l.orders || []) {
      const od = tk.el("div", "muted", "order from " + o.by + (o.tx ? " · tx " + o.tx.slice(0, 12) + "… on " + (NETS[o.chain_id] || o.chain_id) : " · old SRNet-balance order") + (o.note ? " · " + o.note : "") + " ");
      od.appendChild(tk.btn("Check payment", () => tk.send({type: "verify", order: o.id}))); d.appendChild(od);
    }
    r.appendChild(d);
  }
}
tk.onView(render);
