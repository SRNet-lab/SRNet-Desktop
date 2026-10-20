# SRNet client (v0.2 Beta)

One app for SRNet: chat rooms, boards, wikis, mail, private files and other SNApps, over
Session Router.

- **SNApps.** Boards, chat rooms, wikis, storefronts, file drops, profiles and plain-HTML
  `.sesh` pages. Hosts serve only signed data; the client ships the interface. Every record is
  signature-checked, every template runs in a sandbox with no network access, and HTML pages
  open in a viewer that strips all scripts.
- **Mail.** Sealed messages to anyone's SRNet address, stored by mail hosts until they come
  online. Hosts can't read your mail or see who sent it. Senders sign every message.
- **Private files.** File drops are encrypted on your computer before upload. Share with chosen
  people, with anyone who has the link, or publish unencrypted if you choose.
- **Shops.** A storefront shows each item's price with a payment address, the exact amount and
  a QR code. You pay from your own wallet; the client never holds money.
- **Exits (add-on).** Buying time on an SRNet exit and sending your traffic through it is a
  separate add-on, `srnet-exits`, from
  [SRNet-VPSHost-ExitPack](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack). Install it with
  `./install.sh --with-exits`. Without it, this client has no wallet and no payment code.

SRNet is beta software.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

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

## Install

These steps are for a fresh Ubuntu 24.04 desktop (amd64), the supported platform for this
release. We recommend a dedicated machine or drive for SRNet. Run the blocks below in a terminal,
top to bottom. Each block starts in your home folder.

macOS works for SNApps, mail and files; routing traffic through an exit
(`srnet connect`, in the exits add-on) uses nftables and is Linux-only. Windows is not supported in this release.

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
./install.sh --with-exits                       # or plain ./install.sh without the exits add-on
export PATH="$HOME/.local/bin:$PATH"
cd ~
```

The installer needs no root. It puts everything under `~/.local/share/srnet-client` and adds
`srnet` to `~/.local/bin`. `~/SRNet-Desktop/install.sh --uninstall` removes it and keeps your
settings in `~/.srnet`. The `export PATH` line is only needed in this terminal; Ubuntu adds
`~/.local/bin` to new logins automatically. Running `./install.sh` again later upgrades the
client and, if it is installed, the exits add-on.

### 4. First run

```sh
srnet doctor      # check Session Router, DNS, the demo hosts (and the exits add-on, if installed)
srnet ui          # open the app in your browser
```

The app opens with the demo lobby, directory and mail already joined.

### 5. Use an exit (exits add-on)

You need a little USDC on Arbitrum One or Base in any wallet (a phone wallet or an exchange
withdrawal both work), plus a little ETH on the same network for the network fee.

```sh
srnet exits                            # list sellers; note a seller id
srnet buy --seller <id> --hours 1      # shows the address, exact amount and a QR code
                                       # pay it from your wallet; the client waits for the seller
srnet connect <id>                     # route traffic and DNS through the exit (asks for sudo)
curl -s https://api.ipify.org; echo    # prints the exit's IP address
srnet disconnect                       # back to your normal connection
```

The **Exits** tab in the app does the same with buttons. More in the add-on's
[README](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/blob/main/client/README.md).

### Starting and stopping Session Router

Not part of the install; for later, if you don't want Session Router running all the time:

```text
sudo systemctl disable session-router-client   # no autostart at boot
sudo systemctl start session-router-client     # start it yourself
sudo systemctl stop session-router-client      # stop it
```

## The app

`srnet ui` (or plain `srnet`; `srnet snapps` is an alias) is the one app. It opens SNApps on Home
on `127.0.0.1:48920`:

- **Home**: join a collection, your sidebar of rooms, boards, file drops, mail, and **Check my setup**.
- **Add-on tabs**: an installed add-on can add its own tab, such as **Exits** from `srnet-exits`.
  Its backend runs on a token-protected loopback port, and the app reaches it only through
  its own origin.

`srnet doctor` (and Check my setup) runs a read-only preflight: ports, the Session Router API on
`127.0.0.1:1190`, `.sesh` DNS, a demo reachability probe, clock, disk and VPN/firewall, then the
checks of any installed add-on. Each line is PASS/WARN/FAIL with a FIX hint; a report file is
written for support.

The page only answers to the link it prints.

## Command line

```
srnet ui [--config F]            the app; `snapps` is an alias
srnet doctor                     check setup
srnet open <link>                open an srnet:// link (shows the join sheet)
srnet mail ...                   sealed mail (srnet mail -h)
```

With the exits add-on: `srnet exits`, `srnet buy`, `srnet paid`, `srnet connect`,
`srnet status`, `srnet disconnect` and `srnet verify`. Without it, those print how to install it.

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

## Paying in shops

Shop items carry a price in USDC (Arbitrum One or Base) or ETH, and the seller's own address.
**Pay** shows the network, the address, the exact amount and a QR code for a phone wallet
(EIP-681). You pay from your own wallet and pay the network fee; the client never holds or moves
money. Then **I paid: tell the seller** sends the seller an order with the transaction hash. Shop
owners can check a payment with `srnet verify` (exits add-on) or in the block explorer.

Each order gets its own exact USDC amount: the price plus a random few millionths of a USDC
(under one cent). The app never picks one of its own amounts from the past week again. The
amount is random, not unique: two buyers can rarely land on the same one, which is fine because
an order is matched by its transaction. The order names that amount and the transaction. When
the seller checks an order, it only counts if the transaction pays exactly that amount on the
listing's network, and no other order (in any of your stores paid to the same address) names the
same transaction. If two orders name one transaction, both are shown as in dispute so the seller
can ask the buyers. An open payment sheet is remembered for a week, so "I paid" still knows the
exact amount after a restart. Pressing **Pay** again on the same item in that week (after
closing the sheet or reloading the page) opens the same sheet with the same amount, unless the
price or address has changed.

- One payment is limited to 20 USDC by default (`"payments": {"max_usdc": "40"}` in
  `client.json` changes it).
- ETH prices only work on test networks. Real payments are USDC.
- Payments are final; there are no refunds through SRNet.
- Items from shops that still use the old SRNet balance show "seller needs to update".

**Old SRNet balance.** Earlier versions kept USDC in an SRNet balance on Arbitrum One. That is
gone. If you have USDC left there, the exits repository has a small tool to send it back to your
wallet until 2027-01-18:
[contrib/legacy-cashout](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/tree/main/contrib/legacy-cashout).

## Joining collections

The SNApps client starts with Home in the ribbon (plus a tab for each installed add-on). Everything else is something you joined:

- **Join**: on Home, paste a `.sesh` address (lists what that host offers) or an invite link
  `srnet://<name>.sesh/<type>/<collection>#root=<root key>`. The core fetches the host's signed delegation and
  shows the name, type, host and root key; nothing is saved until you click Join. Links are strictly validated
  (only `.sesh` hosts, known types, plain names, a 64-hex root).
- **Sidebar**: joined collections grouped by host, with unread counts. Rename, mute, pin, share and leave from the `⋯`
  menu. State lives in the client's `prefs.json`; no restart or config edit needed.
- **Trust on first use**: the root key is pinned the first time you join. If a host later presents a different root
  key, sync and posting stop and the collection shows a warning. Re-trust is explicit (type "trust the new key") and wipes
  that collection's cache.
- **Share** makes an invite link carrying the pinned root key, so the receiver verifies the same key.
- **Directories** (`directory` type): hosts list their own collection by signing a `directory.entry` with that
  collection's root key (`snapps/scripts/directory_entry.py`); entries signed by anyone else are ignored.
- **Link handler**: `srnet open <link>` opens the join sheet in the running client. `install.sh` registers it for
  `srnet://` on Linux (xdg). macOS needs an app bundle to own a URL scheme,
  so there run `srnet open '<link>'` by hand.

## Related repositories

- [Session-Router-SRNet](https://github.com/SRNet-lab/Session-Router-SRNet): the Session Router
  build used in the install steps above, with exit mapping added.
- [SRNet-VPSHost-ExitPack](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack): the exits
  add-on for this client, and the pack for running your own paid exit and listing it on the
  exit board.

## License

client-cli/ is Apache-2.0 and snapps/ is MIT. See [LICENSE](LICENSE).

## Donations

If SRNet is useful to you, donations are welcome at
`0xec4fEECa354C7eabbe8957a23c361Aa365E3b377` (USDC or ETH on Arbitrum One or Base).
