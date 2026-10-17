"use strict";
function render(v) {
  const r = tk.root(); r.appendChild(tk.el("h2", null, "Store · " + v.collection)); r.appendChild(tk.status(v));
  for (const l of v.view.listings || []) {
    if (!l.active) continue;
    const d = tk.el("div", "item"); d.appendChild(tk.el("h3", null, l.title));
    d.appendChild(tk.el("div", "body", l.desc || ""));
    d.appendChild(tk.el("div", "muted", (l.currency === "SRNET" ? l.price + " USDC · SRNet balance" : "direct " + l.currency + " (testnet)") + " · seller " + l.seller_label + " · " + (l.rating ? "★ " + l.rating + " (" + l.reviews.length + ")" : "no reviews")));
    // The core shows its own payment sheet and signs; this page never builds or signs a payment.
    d.appendChild(tk.btn(l.currency === "SRNET" ? "Pay with SRNet balance…" : "Payment details…", () => tk.send({type: "pay", listing: l.id})));
    const rt = tk.el("input"); rt.placeholder = "rating 1-5"; const tx = tk.el("input"); tx.placeholder = "review";
    d.appendChild(rt); d.appendChild(tx);
    d.appendChild(tk.btn("Review", () => tk.draft("shop.review", {rating: parseInt(rt.value, 10), text: tx.value || null}, [l.id])));
    for (const rv of l.reviews) d.appendChild(tk.el("div", "muted", "★" + rv.rating + " " + rv.by + ": " + rv.text));
    if (l.seller_label && v.me && l.seller && l.seller.slice(0, 6) === v.me) for (const o of l.orders || []) {
      const od = tk.el("div", "muted", "order from " + o.by + " · lease " + o.lease_id + " ");
      od.appendChild(tk.btn("Check payment", () => tk.send({type: "verify", order: o.id}))); d.appendChild(od);
    }
    r.appendChild(d);
  }
}
tk.onView(render);
