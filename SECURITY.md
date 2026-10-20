# Security

## Reporting

Report vulnerabilities through a private security advisory:
https://github.com/SRNet-lab/SRNet-Desktop/security/advisories/new

Please don't open public issues for unfixed admission, signature or fund-loss
bugs.

## Design notes

The SRNet client holds no funds and no wallet key. Shop payments are shown as an
address, an exact amount and a QR code, and are paid from the buyer's own wallet.

Exits, their payments, the kill switch and exit DNS are in the exits add-on and the seller
pack: [SRNet-VPSHost-ExitPack](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack) (see its
README and QUICKSTART.md).

## Known limitations

- Payments are plain on-chain transfers and final. SRNet has no refunds, disputes or
  arbiter. A payment with the wrong amount or on the wrong chain can't be matched to an
  order; buyer and seller have to sort it out themselves.
- Add-ons run as your user with the client's rights. Install only ones you trust.
- Template sandboxing and record signatures protect the app from hosts; they don't make a
  host's content true.
