"""First-run exit-visibility warning (required before connect/buy)."""
from __future__ import annotations

from pathlib import Path

WARNING = """\
SRNet exit visibility warning
=============================
The exit marketplace is Beta / Untested: it works but is not
production-certified. Keep amounts small.

When you connect through an exit, the exit OPERATOR can see:
  • the destinations you reach (IP addresses and ports)
  • connection timing and approximate transfer sizes
  • TLS SNI (the hostname on most HTTPS connections)

They cannot see the contents of properly encrypted traffic (HTTPS), but SNI
and destination IPs are enough to learn which sites you visit.

While connected, every DNS lookup goes through the tunnel to the exit's own
resolver, which resolves it itself (no public resolver involved). The exit
operator can therefore see the names you look up, as well as your
destinations; .sesh names stay on Session Router. The local nftables kill switch blocks clearnet TCP and DNS if
the tunnel drops; other UDP to the internet stays open for the overlay, so apps using
QUIC or WebRTC may bypass the exit.

The deposit fee (up to 3%) is taken when you top up and is NOT
returned on cash-out. Only unused available balance can be cashed out.

Lease payments are FINAL once a lease starts: there is no buyer cancel and
no refund, even if the exit does not work. The client buys one hour at a
time and renews hourly, so the most you can lose is about one hour.

Type exactly:  I UNDERSTAND
"""


def ensure_ack(state_dir: Path) -> None:
    ack = state_dir / "visibility.ack"
    if ack.exists() and ack.read_text().strip() == "I UNDERSTAND":
        return
    print(WARNING)
    try:
        ans = input("> ").strip()
    except EOFError:
        ans = ""
    if ans != "I UNDERSTAND":
        raise SystemExit("refusing to continue without the visibility acknowledgement")
    state_dir.mkdir(parents=True, exist_ok=True)
    ack.write_text("I UNDERSTAND\n")
    ack.chmod(0o600)
