# Donation page

A small page a store can put on its .sesh name, next to the shop, for donations of any amount in
ETH or USDC. The donor pays from their own wallet; the page only reads the chain.

Offered: ETH on Ethereum, Arbitrum One and Base; USDC on Arbitrum One and Base.
Amounts: USDC 0.10 to 10000, ETH 0.00001 to 5.

How it works:

1. The donor picks what to send and types an amount.
2. The page answers with the address, an exact amount and a QR code (an EIP-681 link). The exact
   amount is up to 0.01 USDC (or 0.00001 ETH) more than the typed amount. No two open donations on
   the same chain and asset share an amount. Each amount is held for 7 days.
3. After paying, the donor pastes the transaction hash. The page marks the donation paid only if the
   transaction is on the right chain, succeeded, sends exactly that amount to the payout address
   (for USDC, through the official Circle contract), was mined within the donation's 7 days (from
   5 minutes before it was opened), has enough confirmations (Ethereum 12, Arbitrum 20, Base 10),
   its block is still the chain's block at that height, and the same transaction hasn't paid another
   donation. The check itself can come later: a payment made in time still counts.
4. Two independent public RPCs per chain are asked; both must answer and agree. Their answer about
   a transaction is reused for 30 seconds.
5. If the page can't open a new donation (too many at once), it still shows the address and a QR
   code to send any amount; it just can't confirm that payment.

The SRNet client's 20 USDC payment limit applies to payment sheets the client shows for shop items.
This page is a separate address and QR shown to the donor, so that limit doesn't apply to it.

Limits: native ETH sent through a smart-contract wallet (an internal transfer) can't be checked here,
though it still arrives. A payment of a different amount than asked isn't matched automatically.
A reorg deeper than the confirmation count after a donation was marked paid isn't noticed.
A payment made after its 7 days are up isn't matched. Checks are rate limited per donation, per
caller and in total, so a sustained flood of checks can hold real ones back while it lasts. If either
RPC for a chain is down, checks on that chain wait until it's back. The page and a shop on the same
name keep separate records, so one transaction could be counted by both; that matters only if the
shop ships goods.

Run it on loopback and route `/donate` to it from the store's front:

    python3 -m donate.server --data DIR --pay-to 0xYourAddress --port 48922

JSON for scripts: `POST /donate/api/new` with `{"asset": "eth"|"usdc", "chain_id": 1, "amount": "0.01"}`,
`GET /donate/api/d/<id>`, and `POST /donate/api/d/<id>/check` with `{"tx": "0x..."}`.
