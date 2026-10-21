# Changelog

## [0.2.0] - 2026-10-20

### Added

- Donation page for stores: donations of any amount in ETH (Ethereum, Arbitrum One or Base) or
  USDC (Arbitrum One or Base). Each donation gets its own exact amount and a QR code, and it
  counts as paid once two public RPCs agree the payment arrived. See
  [snapps/docs/DONATE.md](snapps/docs/DONATE.md).
- Exits are an optional add-on, `srnet-exits`, from
  [SRNet-VPSHost-ExitPack](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack). Install it with
  `./install.sh --with-exits`. It also runs on its own, without this client.
- Shops show a payment sheet with the seller's address, the exact amount and a QR code. You pay
  from your own wallet.
- `install.sh` puts `~/.local/bin` on your PATH for new terminals, and `--with-exits` runs
  `srnet setup-router` for you with one sudo prompt.
- README: a file drop screenshot, a note on upcoming storage features and a link to
  [SRNet-TypedSNApp-HostPack](https://github.com/SRNet-lab/SRNet-TypedSNApp-HostPack).

### Changed

- No developer fee anywhere in SRNet.
- Simpler payments: you pay the seller straight from your own wallet. No balance to top up, no
  contract and no escrow.
- The core client has no wallet and no payment code.
- File drop hosts are now called Storage Sellers.
- The README says that mail and private files are encrypted on your computer with
  ChaCha20-Poly1305.

### Removed

- The SRNet balance, top-ups, the relayer and the 3% deposit fee.
- The Wallet and Exits views in the core client. Exits now come from the add-on.
- The walkthrough PDF, for now.

### Fixed

- `state.json` is written atomically and changed through a locked update, so one process can no
  longer drop another's connection or DNS state.
- Donation page: refused requests close the connection, idle connections time out, and the page
  won't start with an empty ledger when it can't read the old one.
- Shops: one exact amount per order, Pay again reuses the open payment sheet, and payment sheets
  stay across restarts.
- A broken add-on no longer stops the core commands.
- `install.sh --uninstall` removes only the exact PATH line it added.

## [0.1.0] - 2026-10-17

First public beta: boards, chat rooms, wikis, stores, file drops, profiles and `.sesh` pages,
sealed mail, encrypted file drops, and exits paid from an SRNet balance.
