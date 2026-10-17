# SRNet client

One app for SRNet: typed SNApps, the exit marketplace, and your SRNet balance.

- **SNApps.** Boards, chat rooms, wikis, storefronts, file drops, profiles and plain-HTML
  `.sesh` pages. Hosts serve only signed data; the client ships the interface. Every record is
  signature-checked, every template runs in a sandbox with no network access, and HTML pages
  open in a viewer that strips all scripts.
- **Exits (Beta / Untested).** Browse exit sellers from the signed exit board, buy an hour,
  auto-renew, and connect. The board is fetched from three `.sesh` names with failover and a
  verified cache. The exit marketplace works but is not production-certified: buy one hour at
  a time and keep amounts small.
- **Wallet / Top up.** Scan the QR with a phone wallet, or send USDC on Arbitrum One from any
  exchange or wallet. A relayer moves it into your SRNet balance. No ETH needed.
- **One way to pay.** Exits, store items and future paid SNApps all use *Pay with SRNet
  balance*, confirmed in the app's own prompt and sent through the same relayer, so paying
  never needs ETH either.
- **Mail.** Sealed messages to anyone's SRNet address, stored by mail hosts until they come
  online. Hosts can't read your mail or see who sent it. Senders sign every message.
- **Private files.** File drops are encrypted on your computer before upload. Share with chosen
  people, with anyone who has the link, or publish unencrypted if you choose.

## Mail

Your mail address is your SRNet identity key plus the 1 to 3 mail hosts you pick, shared as a
`srnet://mail/...` link. Open **Mail** at the top of the SNApps sidebar, or use the command line:

```sh
srnet mail hosts http://<mail host> [http://<backup host>]   # set your hosts and register
srnet mail address --name "Alice"                            # your srnet://mail link to share
srnet mail send 'srnet://mail/...' -s "Hello" -m "Hi Bob"
srnet mail fetch && srnet mail list && srnet mail read <id>
srnet mail list requests && srnet mail accept <id>
srnet mail name 'srnet://room/...' "Bob's room"              # local petname for any srnet:// address
```

- Each message is signed with your key, then sealed to the recipient's key (X25519 +
  ChaCha20-Poly1305) and padded to a size bucket (1 KiB to 64 KiB). A mail host sees only
  the recipient key, the message id, the padded size and the time.
- Messages go to every host on the recipient's list; your client drops duplicates.
- New installs start with one contact, **SRNet** (the team inbox): `srnet mail send SRNet -s "Hi" -m "..."`.
- Hosts keep mail 7 days by default (30 at most) and delete it only after your client has saved it and acknowledged it (fetch, then ack). Mail that is never acknowledged is deleted when it expires. Your
  client keeps it sealed on disk and opens it only to show it.
- Reading an inbox needs a fresh signed challenge from the owner's key. Nobody else can list
  or fetch it.
- Mail from your contacts goes to **Inbox**. Mail from anyone else goes to **Requests**, which
  has tighter limits (32 messages, 16 KiB each, 20 per hour). Accepting a sender moves them
  to your inbox on every host without telling the host who they are.
- **Attach** a file in the Mail page and it is encrypted on your computer and stored on a file
  drop you've joined (no public listing). Only the key-in-link goes inside the sealed message,
  so only your recipients can open it. The size limit is set by the file drop host (25 MiB by default). You can also paste file-drop links.
- Petnames are local only. Links can suggest a name; your own name always wins.

Wire format and host API: [`snapps/docs/MAIL.md`](snapps/docs/MAIL.md).

## Install (Ubuntu 24.04)

Linux is the supported platform for this release; it is tested on a fresh Ubuntu 24.04 desktop
(amd64). We recommend a dedicated machine or drive for SRNet. Run the blocks below in a terminal,
top to bottom. Each block starts in your home folder.

### 1. Session Router

SRNet runs on top of Session Router, which carries all `.sesh` traffic. Install the Session
Foundation's `session-router-client` service together with the SRNet build of the router binary
(the Foundation's Session Router plus exit mapping, so `srnet connect` can send your traffic through
an exit; source and build details:
[Session-Router-SRNet](https://github.com/SRNet-lab/Session-Router-SRNet), branch `srnet/mapexit-dev`).

```sh
cd ~
sudo apt update
sudo apt install -y curl ca-certificates
sudo curl -fsSLo /usr/share/keyrings/session-foundation.gpg https://deb.session.foundation/pub.gpg
echo "deb [signed-by=/usr/share/keyrings/session-foundation.gpg] https://deb.session.foundation $(. /etc/os-release; echo $VERSION_CODENAME) main" | sudo tee /etc/apt/sources.list.d/session-foundation.list
sudo apt update
cd /tmp
curl -fLO https://github.com/SRNet-lab/Session-Router-SRNet/releases/download/v1.1.0-srnet6/session-router-srnet_1.1.0+srnet6_amd64.deb
echo "5c1c9b553b25649657f9f8e3ae0431aa3fe0534c82e65214588c728307bf3237  session-router-srnet_1.1.0+srnet6_amd64.deb" | sha256sum -c
sudo apt install -y ./session-router-srnet_1.1.0+srnet6_amd64.deb session-router-client
sudo apt-mark hold session-router-srnet session-router-client
cd ~
```

`sha256sum -c` must print `OK`; if it does not, stop and re-download. The hold keeps apt from
swapping the stock router binary back in on an upgrade. (If you had installed the stock
`session-router-bin` before, apt may now list `libsessionrouter1.1`, `liboxen-quic1.12` and similar
as no longer required. That is fine: the SRNet build does not use them, so `sudo apt autoremove`
is safe.)

The package sets up `session-router-client.service`, starts it now and enables it at boot (see
[Starting and stopping Session Router](#starting-and-stopping-session-router) to change that later).

### 2. `.sesh` names

Ubuntu's resolver (systemd-resolved) does not send `.sesh` names to Session Router on its own.
Add this once so it does, every time the service starts:

```sh
cd ~
sudo mkdir -p /etc/systemd/system/session-router-client.service.d
printf '[Service]\nExecStartPost=+/bin/sh -c "sleep 3; resolvectl dns sr-tun0 127.3.2.1; resolvectl domain sr-tun0 ~sesh ~snode; resolvectl default-route sr-tun0 false"\n' | sudo tee /etc/systemd/system/session-router-client.service.d/dns.conf
sudo systemctl daemon-reload
sudo systemctl restart session-router-client
```

Programs look names up through `/etc/resolv.conf`. On a stock Ubuntu desktop it already points
at systemd-resolved (`nameserver 127.0.0.53`). Some cloud and VPS images ship a fixed file with
the provider's DNS servers instead, so `.sesh` lookups never reach systemd-resolved. This links it
back only if `127.0.0.53` is missing (your normal DNS keeps working through resolved):

```sh
grep -q '^nameserver 127.0.0.53' /etc/resolv.conf || sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
grep nameserver /etc/resolv.conf   # now shows 127.0.0.53
```

Only `.sesh` and `.snode` names go to Session Router; all other DNS is unchanged (while
`srnet connect` is up, srnet sends all of it to the exit's own resolver through the tunnel). Check
that the router API is listening and `.sesh` names resolve:

```sh
ss -ltn | grep 1190           # something listening on 127.0.0.1:1190
getent hosts localhost.sesh   # prints an fd2e:... address
```

Other systems: see the upstream install guide at
https://github.com/session-foundation/session-router/blob/dev/docs/install.md

### 3. The SRNet client

```sh
cd ~
sudo apt install -y git python3-venv nftables   # nftables: the kill switch used by srnet connect
git clone https://github.com/SRNet-lab/SRNet-Desktop.git
cd SRNet-Desktop
./install.sh
export PATH="$HOME/.local/bin:$PATH"
cd ~
```

The installer needs no root. It puts everything under `~/.local/share/srnet-client` and adds
`srnet` and `srnet-relayer` to `~/.local/bin`. `~/SRNet-Desktop/install.sh --uninstall` removes it
and keeps your wallet in `~/.srnet`. The `export PATH` line is only needed in this terminal; Ubuntu
adds `~/.local/bin` to new logins automatically.

### 4. First run

```sh
srnet key new     # create your wallet; asks you to choose a keystore passphrase (back up the file and passphrase)
srnet doctor      # check Session Router, DNS, the demo hosts, your wallet and the relayer
srnet ui          # open the app in your browser
```

`srnet key new` asks for a keystore passphrase twice. It encrypts your wallet file; you need it
whenever you use the wallet, and it cannot be recovered if lost.

The app opens with the demo lobby, directory and mail already joined. Top-ups and payments go
through the SRNet demo relayer on a `.sesh` name by default, so there is nothing to set up and you
never need ETH (see [Paying](#paying)).

### 5. Fund your wallet and use an exit

In the app's **Wallet** tab (or `srnet topup --amount 2`), send about 2 USDC on Arbitrum One to the
address it shows. One hour of exit time costs what the seller lists (the demo exits charge 0.50
USDC), plus the 3% deposit fee and small relayer fees.

```sh
srnet exits                            # list sellers; note a seller id
srnet buy <seller id>                  # prepay one hour from your SRNet balance
srnet connect <seller id>              # route traffic and DNS through the exit (asks for sudo)
curl -s https://api.ipify.org; echo    # prints the exit's IP address
srnet disconnect                       # back to your normal connection
curl -s https://api.ipify.org; echo    # your own IP again
```

The **Exits** tab in the app does the same with buttons.

### Starting and stopping Session Router

Not part of the install; for later, if you don't want Session Router running all the time:

```text
sudo systemctl disable session-router-client   # no autostart at boot
sudo systemctl start session-router-client     # start it yourself
sudo systemctl stop session-router-client      # stop it
```

macOS works for SNApps, mail, files and the wallet; routing traffic through an exit
(`srnet connect`) uses nftables and is Linux-only. Windows is not supported in this release.

## The app

`srnet ui` (or plain `srnet`; `srnet snapps` is an alias) is the one app. It opens SNApps on Home
on `127.0.0.1:48920`, with three ribbon tabs in the same window and style:

- **Home**: join a collection, your sidebar of rooms, boards, file drops, mail, and **Check my setup**.
- **Exits (Beta / Untested)**: the signed seller board (region, price per hour, last refresh), buy
  one hour from your SRNet balance (final, passphrase asked in a sheet), connect, disconnect,
  auto-renew and lease status.
- **Wallet**: your SRNet balance, the top-up QR and address, the relayer setting (optional), and
  any USDC waiting in your wallet to be deposited.

The wallet backend runs inside the same process on a token-protected loopback port; the shell
reaches it only through the core's API. The old standalone wallet page is retired
(`srnet ui --legacy-page` keeps it for now).

`srnet doctor` (and Check my setup) runs a read-only preflight: ports, the Session Router API on
`127.0.0.1:1190`, `.sesh` DNS, a demo reachability probe, clock, disk and VPN/firewall, then your
wallet, SRNet balance and relayer. Each line is PASS/WARN/FAIL with a FIX hint; a report file is written for support.

The page only answers to the link it prints, and your keystore passphrase is used for the
action you click and never saved.

## Command line

```
srnet exits                      exit marketplace (Beta / Untested): list sellers
srnet buy <seller>               prepay one hour from your SRNet balance (--hours N for more)
srnet renew --loop               buy the next hour near the end of the current one
srnet connect <seller>           route traffic and DNS through the exit (Linux; sudo for the kill switch and DNS)
srnet topup --amount N           top up: QR or send USDC from any wallet, gas-free deposit
srnet cashout --amount N         withdraw unused SRNet balance (gas-free)
srnet pay --vendor-id V --amount N   pay any SRNet seller from your balance
srnet vendor --shop-key K        seller: set up SRNet-balance payments
srnet deliver                    resend your exit claim to the seller
srnet status | stop | disconnect
srnet ui [--config F]            the app (SNApps + Exits + Wallet); `snapps` is an alias
srnet key new|import|show        your encrypted wallet
srnet doctor                     check setup
```

Details: [client-cli/README.md](client-cli/README.md).

## Files

Uploads are encrypted by default (ChaCha20-Poly1305, a new random key per file, sizes padded to
buckets, name and type sealed inside). The host stores only ciphertext. Choose per file:

- **Only me and people I choose** (default): the file key is sealed to each person's key from
  their SRNet profile. Add people later, or approve someone's access request.
- **Anyone with the link**: the key rides in the link after `#`, so the host never sees it.
- **Not encrypted**: for content meant to be public and indexable.

Revoking someone stops them getting future versions. It can't take back a copy they already
downloaded.

Each file drop host sets its own largest file size (25 MiB by default) and may set a storage
quota per uploader. The client reads the limit from the host's signed policy and refuses a
larger file before uploading anything. Big files go up in 1 MiB chunks. If an upload is
interrupted, uploading the same file again continues where it stopped.

## Moderation, room headers and pins

Hosts can act on content in three ways, each a signed, public record:

- **Hide**: the post stays on the host and keeps syncing. Your client honours hides by default,
  and you can choose to show a hidden post anyway.
- **Remove**: for illegal content, doxxing or credible threats. The host deletes the post text,
  or a file's bytes, at once. Mirrors and clients delete their copies when they next sync. The
  post shows as "Removed by host: <reason>", and no setting brings it back.
- **Pin**: up to 3 posts shown at the top of a room or board.

Rooms also show a host-signed header with the room's description and rules.

## Paying

Your SRNet balance lives in the SRNetBalance contract on Arbitrum One at
`0x7E0d01B7dd916BdAB96B0971ab92D160E1664431`. The source is verified on Sourcify.
The contract is unaudited; keep amounts small.

How funding and paying work:

1. **Top up.** Scan the QR in Wallet (or `srnet topup`) with a phone wallet, or send USDC on
   Arbitrum One to your SRNet wallet address from any exchange or wallet. Send only USDC on
   Arbitrum One.
2. **Deposit.** When it arrives, the client signs a gas-free approval and the relayer moves it
   into your SRNet balance.
3. **Pay.** Exits, store items and cash-outs are signed by your wallet and submitted by the same
   relayer. You never need ETH.

Fees:

- Deposit fee: 3% of each deposit, not refunded on cash-out.
- Relayer fee: a flat fee per relayed action, taken from your SRNet balance: 0.05 USDC per
  deposit and 0.02 USDC per payment, hourly renewal or cash-out by default. The contract caps it
  at 1 USDC. Your signature binds the amount, the recipient and the fee, so the relayer can't
  move your funds anywhere else or charge more than you signed.
- Leases are prepaid whole hours, at most $20 per lease. Payment is final once a lease starts.
  Buying one hour at a time keeps the most you can lose to about one hour.
- Store items are paid the same way: one payment from your SRNet balance to the seller, at most
  $20, final, with no fee beyond the relayer fee. Sellers run `srnet vendor --shop-key <key>` once and put the vendor id
  in their listings. Store payments from the SNApps window stay off on mainnet until you set
  `"payments": {"allow_mainnet": true}` in `client.json`.
- After you buy exit time, the client sends your signed claim straight to the seller over
  Session Router and waits for the seller's acknowledgment. `srnet deliver` resends it.

**Relayer.** Top-ups, payments, renewals and cash-outs go through a relayer, which pays the gas.
The client uses the SRNet demo relayer by default, reached only over Session Router at
`http://srnet7fqaajaqb6u7ho56znknysg5ime6kfbaymaaih4ba3q1ndo.sesh/relayer`. To use another one, enter
its URL in the Wallet tab (or put `"relayer": "<url>"` in `~/.srnet/config.json`, or set
`SRNET_RELAYER`). Anyone can run one with `srnet-relayer` (see
[client-cli/README.md](client-cli/README.md)). With `"relayer": "off"`, payments are sent from your
own wallet instead, which needs a little ETH on Arbitrum One for gas.

The previous contract `0x07bbe156083EbBcc9435C721393Edd36a383E648` stays live, so balances
there can still be cashed out.

## Testing

```sh
python3 -m unittest discover -s client-cli/tests           # exits, wallet, top-up, UI
(cd snapps/tests && python3 -m unittest)                    # typed SNApps
python3 scripts/smoke.py                                    # local end-to-end, loopback only
```

The smoke test runs the SNApps demo, lists exits from a locally signed board with a dead first
copy, and does a full QR top-up on a local anvil chain. The client tests also run a gas-free
deposit, payment, renewal and cash-out from a wallet holding no ETH (anvil, when Foundry is installed). It uses throwaway keys only.

## Known gaps

- Claim delivery needs sellers running the updated seller pack (`watcher --claim-listen`).
- SNApp card verification is optional and not bundled; card views report that.

## Security

See [SECURITY.md](SECURITY.md).

## License

Apache-2.0 for the exits client and contract, MIT for the SNApps client. See [LICENSE](LICENSE).

## Joining collections

The SNApps client starts with Home, Exits and Wallet in the ribbon. Everything else is something you joined:

- **Join**: on Home, paste a `.sesh` address (lists what that host offers) or an invite link
  `srnet://<name>.sesh/<type>/<collection>#root=<root key>`. The core fetches the host's signed delegation and
  shows the name, type, host and root key; nothing is saved until you click Join. Links are strictly validated
  (only `.sesh` hosts, known types, plain names, a 64-hex root).
- **Sidebar**: joined collections grouped by host, with unread counts. Rename, mute, pin, share and leave from the `⋯`
  menu. State lives in the client's `prefs.json`; no restart or config edit needed.
- **Trust on first use**: the root key is pinned the first time you join. If a host later presents a different root
  key, sync and posting stop and the collection shows ⚠. Re-trust is explicit (type "trust the new key") and wipes
  that collection's cache.
- **Share** makes an invite link carrying the pinned root key, so the receiver verifies the same key.
- **Directories** (`directory` type): hosts list their own collection by signing a `directory.entry` with that
  collection's root key (`snapps/scripts/directory_entry.py`); entries signed by anyone else are ignored.
- **Link handler**: `srnet open <link>` opens the join sheet in the running client. `install.sh` registers it for
  `srnet://` on Linux (xdg). macOS needs an app bundle to own a URL scheme,
  so there run `srnet open '<link>'` by hand.
- Collections from an existing `client.json` still load, as pre-joined and trusted with their configured pin.
