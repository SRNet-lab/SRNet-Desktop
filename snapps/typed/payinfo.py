"""What a store payment looks like: the official USDC contract per chain, amounts, and the EIP-681
payment link most wallets open from a QR code.

The core never sends or checks a payment. It shows the seller's address, the exact amount and a QR
code; the buyer pays from their own wallet. Checking that a payment arrived is left to the exits
add-on (srnet-exits), if installed, or to a block explorer.
"""
from __future__ import annotations

import re
from decimal import Decimal

# Official (Circle) USDC, 6 decimals.
USDC = {
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",   # Arbitrum One
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",    # Base
    421614: "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",  # Arbitrum Sepolia (test)
    84532: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",   # Base Sepolia (test)
}
LABEL = {42161: "Arbitrum One", 8453: "Base", 421614: "Arbitrum Sepolia", 84532: "Base Sepolia",
         11155111: "Sepolia"}
EXPLORER = {42161: "https://arbiscan.io/tx/", 8453: "https://basescan.org/tx/",
            421614: "https://sepolia.arbiscan.io/tx/", 84532: "https://sepolia.basescan.org/tx/",
            11155111: "https://sepolia.etherscan.io/tx/"}
TESTNETS = {421614, 84532, 11155111}
DEFAULT_MAX_USDC = "20"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
USDC_RE = re.compile(r"^(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?$")


class PayError(ValueError):
    pass


def label(chain_id: int) -> str:
    return LABEL.get(chain_id, f"chain {chain_id}")


def usdc_units(price: str) -> int:
    """'2.50' -> 2500000 (USDC has 6 decimals)."""
    if not USDC_RE.match(str(price)):
        raise PayError("USDC price must be a number with at most 6 decimals")
    v = int(Decimal(str(price)) * 1_000_000)
    if v <= 0:
        raise PayError("price must be more than 0")
    return v


ORDER_ID_MAX = 9999  # shop payments: price + 1..9999 millionths of a USDC, so each order's amount is its own


def order_units(units: int, rng=None, exclude=()) -> int:
    """The exact amount a buyer sends for one order: the price plus a random few millionths.
    Amounts in `exclude` (this app's own recent ones) are skipped. It is random, not unique:
    another buyer can land on the same amount, which is fine because orders are matched by
    their transaction."""
    import secrets
    pick = rng or secrets.randbelow
    exclude = set(exclude)
    for _ in range(1000):
        u = int(units) + 1 + pick(ORDER_ID_MAX)
        if u not in exclude:
            return u
    free = [int(units) + 1 + i for i in range(ORDER_ID_MAX) if int(units) + 1 + i not in exclude]
    return free[pick(len(free))] if free else u


def order_amount_ok(price_units: int, paid_units: int) -> bool:
    return int(price_units) < int(paid_units) <= int(price_units) + ORDER_ID_MAX


def fmt_usdc(units: int) -> str:
    s = f"{Decimal(units) / Decimal(1_000_000):f}"
    whole, _, frac = s.partition(".")
    frac = frac.rstrip("0")
    return whole + "." + (frac + "00")[:max(2, len(frac))]


def eth_wei(price: str) -> int:
    if not re.fullmatch(r"(0|[1-9][0-9]{0,11})(\.[0-9]{1,18})?", str(price)):
        raise PayError("bad ETH amount")
    return int(Decimal(str(price)) * 10**18)


def usdc_link(chain_id: int, to: str, units: int) -> str:
    if chain_id not in USDC:
        raise PayError(f"USDC is not supported on {label(chain_id)}")
    if not ADDR_RE.match(to):
        raise PayError("bad address")
    return f"ethereum:{USDC[chain_id]}@{chain_id}/transfer?address={to}&uint256={int(units)}"


def eth_link(chain_id: int, to: str, wei: int) -> str:
    if not ADDR_RE.match(to):
        raise PayError("bad address")
    return f"ethereum:{to}@{chain_id}?value={int(wei)}"


def tx_link(chain_id: int, tx: str) -> str:
    e = EXPLORER.get(chain_id)
    return e + tx if e else ""


def max_units(config: dict) -> int:
    """Per-payment limit from client.json "payments": {"max_usdc": "20"} (default 20 USDC)."""
    v = ((config or {}).get("payments") or {}).get("max_usdc") or DEFAULT_MAX_USDC
    try:
        return usdc_units(str(v))
    except PayError:
        return usdc_units(DEFAULT_MAX_USDC)


def have_qr() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


def qr_svg(data: str) -> bytes | None:
    """SVG QR code if the qrcode package is installed, else None (the link and address still work)."""
    try:
        import io
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    buf = io.BytesIO()
    qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2).save(buf)
    return buf.getvalue()
