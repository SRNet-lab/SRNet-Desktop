# Donation page

A small page a store can put on its .sesh name, next to the shop, for donations of any amount in
ETH or USDC. The donor pays from their own wallet; the page only reads the chain.

Offered: ETH on Ethereum, Arbitrum One and Base; USDC on Arbitrum One and Base.
Amounts: USDC 0.10 to 10000, ETH 0.00001 to 5.

How it works:

1. The donor picks what to send and types an amount.
2. The page answers with the address, an exact amount and a QR code (an EIP-681 link). The exact
   amount is up to 0.01 USDC (or 0.00001 ETH) more than the typed amount.
3. After paying, the donor pastes the transaction hash. The page marks the donation paid once the
   transaction sends exactly that amount to the payout address within 7 days and has enough
   confirmations.
4. If the page can't open a new donation (too many at once), it still shows the address and a QR
   code to send any amount; it just can't confirm that payment.

Limits: native ETH sent through a smart-contract wallet can't be checked here, though it still
arrives. A payment of a different amount than asked, or after its 7 days, isn't matched.

Run it on loopback and route `/donate` to it from the store's front:

    python3 -m donate.server --data DIR --pay-to 0xYourAddress --port 48922

JSON for scripts: `POST /donate/api/new` with `{"asset": "eth"|"usdc", "chain_id": 1, "amount": "0.01"}`,
`GET /donate/api/d/<id>`, and `POST /donate/api/d/<id>/check` with `{"tx": "0x..."}`.
