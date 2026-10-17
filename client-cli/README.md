# srnet

The SRNet client: `srnet ui` (the app), the wallet, and the exit marketplace (Beta / Untested)
from the command line. For the normal install, use `./install.sh` at the top of this repository
(see the main [README](../README.md)); this page covers the command line in detail.

Session Router is a separate prerequisite: install and run it yourself. Its API must answer on
`127.0.0.1:1190`; `srnet doctor` checks it. This client never installs Session Router or sets it
up as a service.

The SRNetBalance contract is unaudited and live on Arbitrum One with real USDC. Use small
amounts.

## Install without install.sh

Needs Python 3.10+.

```sh
cd client-cli
pip install .
srnet doctor
```

Dependencies: `cryptography`, `pyzmq`, `eth-account`, `eth-abi` (and `qrcode` for the QR).
Chain access is plain JSON-RPC over HTTPS; no Foundry needed. Without installing,
`python3 -m srnet` (or `srnet.sh`) works from this directory once the dependencies are present.

Settings are read from flags, then `$SRNET_HOME/config.json` (`rpc`, `relayer`, `keystore`,
`listings_path`, `board_urls`, `board_pin`, `sesh_dns`, `spool`, `omq`), then the environment
(`SRNET_RPC`, `SRNET_RELAYER`). `$SRNET_HOME` defaults to `~/.srnet`.

## Wallet

```sh
srnet key new        # new wallet, saved encrypted
srnet key import     # encrypt an existing key (prompted, not echoed)
srnet key show
```

The keystore is `$SRNET_HOME/keystore.json` (V3, scrypt; `--keystore` to override). The
passphrase is prompted, or read from `SRNET_KEYSTORE_PASSWORD` for `renew --loop` run
unattended. `--private-key` / `SRNET_PRIVATE_KEY` still work, in plaintext, with a warning.
`srnet doctor` shows which one is in use. Back up the keystore file and passphrase: there is
no recovery.

## Funding and paying (no ETH needed)

1. Relayer: the client uses the SRNet demo relayer (`chain.DEFAULT_RELAYER`, on a `.sesh` name,
   so Session Router must be up) for the live contract. To use another, set it in the Wallet tab
   in `srnet ui`, `"relayer": "<url>"` in `config.json`, or `SRNET_RELAYER`; `"off"` disables it.
2. Top up: `srnet topup --amount 10` (or Wallet / Top up) shows a QR code and the plain address
   of **your own** SRNet wallet. Scan it with a phone wallet, or send USDC on Arbitrum One from
   any exchange or wallet. When the USDC arrives, the client signs a gas-free approval and the
   relayer deposits it into your SRNet balance.
3. Pay: `srnet buy`, `srnet pay`, hourly auto-renew and `srnet cashout` sign an intent with your
   wallet and the same relayer submits it.

Fees: 3% of each deposit (not refunded on cash-out), plus a flat relayer fee from your SRNet
balance: 0.05 USDC per deposit and 0.02 USDC per payment, renewal or cash-out by default,
capped at 1 USDC by the contract. Your signature binds the amount, the recipient and the fee,
so a relayer can't redirect funds or charge more than you signed.

With the relayer off, `srnet topup` prints the signed request so you can hand it to any relayer
(POST `/fund`), and payments fall back to direct transactions from your wallet, which need a
little ETH on Arbitrum One for gas. `srnet buy --fund` is the same kind of advanced, direct
path.

### Running a relayer

Anyone can run one. It holds only its own gas key and the relay fees it earns:

```sh
SRNET_RELAYER_KEY=0x... srnet-relayer --contract 0x7E0d01B7dd916BdAB96B0971ab92D160E1664431 \
  --rpc https://arb1.arbitrum.io/rpc --host 127.0.0.1 --port 8645
```

It serves `POST /fund`, `/lease`, `/renew` and `/withdraw` (also under a path prefix such as
`/relayer/fund`) and `GET /` (its address, chain and contract), simulates every transaction before
sending it, and enforces per-IP and per-wallet rate limits, a minimum fee and a daily gas budget.
On Arbitrum One it refuses to start unless `SRNET_RELAYER_MAINNET=1` is set.

The simplest way to share it is on a `.sesh` name: keep it on loopback and add it as a route
(`/relayer` -> 8645) in the SRNet host pack's `srnet-host front`, then start it with
`--trust-proxy` so the per-IP limit uses each caller's Session Router address. Otherwise put it
behind HTTPS before sharing its URL.

## Usage

```
srnet key new|import|show       manage the encrypted wallet keystore
srnet doctor                    check Session Router, DNS, packages, chain, wallet, balance, relayer
srnet ui [--no-browser]         the app: SNApps, Exits, Wallet / Top up (also plain `srnet`)
srnet topup --amount N          top up: QR or send USDC from any wallet, gas-free deposit
srnet cashout --amount N        withdraw unused SRNet balance to your wallet (or --to)
srnet pay --vendor-id V --amount N   pay any SRNet seller from your SRNet balance

Exit marketplace (Beta / Untested):
srnet exits [--listings FILE]   list sellers from the signed board (or a local file)
srnet buy <seller> [--hours N]  prepay N hours (default 1) from your SRNet balance, send the claim
srnet connect <seller>          MapExit full_tunnel; unmap and stop renewing if the exit doesn't work
                                (again after disconnect: reconnects inside the paid lease, no new purchase)
srnet renew [--loop]            buy the next hour in the last 10 minutes of the current one
srnet stop                      stop auto-renew; the current hour runs out
srnet status
srnet disconnect                restore DNS, unmap, remove the kill switch, stop auto-renew (the paid lease keeps running)
srnet deliver                   resend your last claim to the seller
```

Exits are Beta / Untested: they work but are not production-certified. Example:

```sh
srnet topup --amount 5          # once; or Wallet / Top up in srnet ui
srnet exits
srnet buy <seller>.sesh         # your own .sesh address is read from Session Router
srnet connect <seller>.sesh     # kill switch + DNS through the exit (sudo asks for your password), then routes
srnet renew --loop
srnet status
srnet disconnect
srnet connect <seller>.sesh     # back on within the paid lease: no new purchase
```

`--client-sesh ADDRESS.sesh` overrides the address `buy`/`connect` read from Session Router
(`localhost.sesh`). `connect` after `disconnect` reuses the recorded lease while it is still
running on chain; once it has ended (or the seller stopped it) it refuses and tells you to buy.

Payment is final once a lease starts. There is no buyer cancel and no refund.
Keep `--hours` at 1 and let auto-renew extend it. If mapping fails or the exit
never becomes capable, the client unmaps and turns auto-renew off.

## Board

`srnet exits` fetches `board.json` and `board.sig` from these names, in order,
through Session Router:

```
exitsrjnc83har7a17mdtjpgi1j3odgiphaffe9ejfq4wnbdxkio.sesh
exitstaf5qs8so5g4ykzodhiyh79zjy3gqmszyzyhhm6r4szpwco.sesh
exitsdr3hzeqcwon7mqa7z87h8rn1j786uabdq5wzjw11us1nroo.sesh
```

The board must be signed by `qbhfd6kc4qijpdpjkd8nfcq55r377m1ff3o6ontfyikygsycywfo.sesh`.
Listings older than 3 hours, or with a bad seller signature, are dropped. If no
copy verifies, the last good board in `$SRNET_HOME/board-cache.json` is used
until it expires. `.sesh` names go through the system resolver first, then the
router's DNS listener at `sesh_dns` (default `127.3.2.1:53`, Session Router's stock listener; no need to set it).

Output:

```
SELLER                                                   REGION  USDC/HR  LAST REFRESH
exit5...sesh                                             US      0.50     12m ago
```

## UI

`srnet ui` serves the app on `127.0.0.1:48920` and opens it in your browser. It has three tabs:
SNApps (Home), Exits (Beta / Untested) and Wallet / Top up. The page answers only to the link it
prints; other local pages can't drive it. The keystore passphrase is typed in for each action and
isn't saved. Turning auto-renew on keeps a `srnet renew --loop` running while the app is open.
`install.sh` adds an **SRNet** menu entry on Ubuntu.

`connect` arms the nftables kill switch, which needs the `nft` tool (`sudo apt install nftables`)
and root. As a normal user `srnet connect` runs `nft` through sudo, which asks for your password
in the terminal. The UI has no terminal, so its Connect button can only arm the kill switch with
passwordless sudo; otherwise it reports the error and does not connect: run `srnet connect` in a
terminal instead. Without nftables, or to connect knowingly without a kill switch, use
`srnet connect <seller> --no-killswitch` (`srnet doctor` flags a missing `nft` as FAIL).

## Claims

After `buy`, the client writes a signed claim (`hold-<id>.json`) to `--spool` and sends it to
the seller over Session Router, waiting for the seller's acknowledgment. `srnet deliver` resends
it (safe to repeat). Sellers need the current seller pack (`watcher --claim-listen`).

## Defaults

- An nftables kill switch (table `inet srnet_ks`) drops clearnet TCP and DNS that does not
  leave through the tunnel interface (`tun`, default `sr-tun0`). UDP to the internet stays
  open for the overlay, so QUIC or WebRTC apps can bypass the exit. `connect` refuses to run
  if the kill switch cannot be armed, unless you pass `--no-killswitch` (or `--allow-no-ks`).
- A first-run warning about what the exit operator can see. Type
  `I UNDERSTAND` to continue.
- DNS goes to the exit's own resolver while connected. Each exit runs a recursive resolver
  (unbound, resolving from the root servers itself, no query logs) at an address inside the
  tunnel, advertised in its listing as `dns`; no public resolver (Cloudflare, Quad9, ...) is in
  the path. systemd-resolved would otherwise send lookups to your normal DNS server on your
  normal interface (a leak, with or without the kill switch). So `connect` starts a small
  forwarder as you (no root, `127.3.2.2:5053`): `.sesh`/`.snode` go to Session Router,
  everything else to the exit's resolver on a socket bound to `sr-tun0`, so it can only travel
  inside the tunnel. Then, in the same sudo session as the kill switch, it points resolved's
  `sr-tun0` link at the forwarder with routing domain `~.` and DefaultRoute. `disconnect` puts
  the link's previous DNS servers, domains and default-route back exactly (saved in
  `state.json`) and stops the forwarder. If the exit's resolver, the forwarder or the tunnel
  stops answering, lookups fail; nothing falls back to your normal DNS or a third party.
  The exit operator can see the names you look up (as well as your destinations).
- Exits without a resolver (older seller packs) show `EXIT DNS NONE` in `srnet exits`;
  `buy` and `connect` refuse them before anything is paid or mapped, unless you pass
  `--system-dns`, which leaves your DNS alone (it then goes out your normal connection).
  `connect` also refuses without systemd-resolved, unless `--system-dns`.
- Default chain is Arbitrum One with the live contract. Testnets are opt-in
  via `--rpc`; other chain ids are refused.

## vendor/

Copies of modules from
[SRNet-VPSHost-ExitPack](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack): the
listing verifier, the claim format and the connect flow. Don't edit them here.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
