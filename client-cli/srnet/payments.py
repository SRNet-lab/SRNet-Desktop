"""One "Pay with SRNet balance" flow for every SRNet use: exits, store items, paid files, rooms.

Every payment is a prepaid one-hour lease to the seller's vendor id on the live SRNetBalance
contract (no contract change):

    startLeaseFor / startLease(vendorId, ratePerHour = price, hoursBought = 1)

- The 3% fee was already taken at deposit. With a relayer set (the normal path) the payment is
  gasless: the wallet signs a StartLease intent and the relayer submits startLeaseFor, taking a
  small relay fee from the SRNet balance. Without one, startLease is sent directly (needs ETH).
- The contract caps each payment at maxPerLease ($20).
- Payment is final: the first hour begins at once, so the seller can claim the whole amount
  right away (claimEarned); the buyer can't cancel.
- The seller proves who they are by signing the listing that carries the vendor id; the
  vendor id is bound on-chain to the seller's wallet (configureVendor).

Callers build a Payment, show describe(p) in their own trusted confirm prompt (the typed core's
sheet, the CLI's [y/N], the UI's confirm dialog), and only then call pay(). Templates never sign.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import chain

MAX_PER_PAYMENT = 20 * chain.USDC   # mirrors maxPerLease on the live contract
PURPOSES = ("exit", "shop", "file", "room")
VENDOR_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class PaymentError(Exception):
    pass


@dataclass
class Payment:
    purpose: str            # exit | shop | file | room
    vendor_id: str          # bytes32 hex from the seller's signed listing
    amount: int             # USDC base units (6 decimals)
    item: str = ""          # what is being bought, for the prompt
    seller: str = ""        # seller label, for the prompt
    ref: str = ""           # listing / record id
    hours: int = 1          # exits may buy more hours; store items always 1
    extra: dict = field(default_factory=dict)

    def check(self) -> None:
        if self.purpose not in PURPOSES:
            raise PaymentError(f"unknown payment purpose {self.purpose!r}")
        if not VENDOR_RE.match(self.vendor_id or ""):
            raise PaymentError("listing has no valid vendor id")
        if int(self.amount) < 1:
            raise PaymentError("amount must be positive")
        if int(self.hours) < 1:
            raise PaymentError("hours must be >= 1")
        if int(self.amount) * int(self.hours) > MAX_PER_PAYMENT:
            raise PaymentError(f"over the {MAX_PER_PAYMENT // chain.USDC} USDC per-payment limit")


def usdc(amount_str: str) -> int:
    """'1.25' -> 1250000. At most 6 decimals."""
    if not re.fullmatch(r"(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?", amount_str or ""):
        raise PaymentError("price must be a USDC amount with at most 6 decimals")
    whole, _, frac = amount_str.partition(".")
    return int(whole) * chain.USDC + int((frac + "000000")[:6])


def describe(p: Payment) -> list[str]:
    p.check()
    total = p.amount * p.hours
    lines = [f"pay: {total / chain.USDC:.2f} USDC from your SRNet balance",
             f"for: {p.item or p.purpose}"]
    if p.seller:
        lines.append(f"seller: {p.seller}")
    if p.hours > 1:
        lines.append(f"hours: {p.hours} at {p.amount / chain.USDC:.2f}/hour")
    lines += ["no extra fee beyond the small relayer fee (the 3% deposit fee was taken at top-up)",
              "payment is final; there is no refund"]
    return lines


def pay(ch, p: Payment) -> dict:
    """Spend from the SRNet balance. Call only after the user confirmed describe(p)."""
    p.check()
    lease = ch.start_lease(p.vendor_id, int(p.amount), int(p.hours))
    return {"ok": True, "lease_id": int(lease), "amount": int(p.amount) * int(p.hours),
            "contract": ch.contract, "buyer": ch.address()}


def verify(ch, lease_id: int, vendor_id: str, amount: int, buyer: str | None = None) -> bool:
    """Seller-side (read-only): did `buyer` pay at least `amount` to `vendor_id` in lease_id?"""
    info = ch._call("leaseInfo(uint256)",
                    ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256", "bool"],
                    int(lease_id))
    extra = ch._call("leaseExtra(uint256)", ["bytes32", "bool", "uint256"], int(lease_id))
    vid = extra[0] if isinstance(extra[0], (bytes, bytearray)) else bytes.fromhex(str(extra[0])[2:])
    ok = vid.hex() == vendor_id.lower().removeprefix("0x") and int(info[2]) >= int(amount)
    if buyer:
        ok = ok and str(info[0]).lower() == buyer.lower()
    return ok


def vendor_id_for(wallet: str, salt_hex: str) -> str:
    """keccak256(wallet ++ salt): the id configureVendor(vendorId, salt) accepts from `wallet`."""
    from .chain import keccak
    return "0x" + keccak(bytes.fromhex(wallet[2:]) + bytes.fromhex(salt_hex.removeprefix("0x"))).hex()


def shop_salt(seller_key_hex: str) -> str:
    """Deterministic salt for a store seller: keccak('srnet-shop/v1' ++ Ed25519 seller key)."""
    from .chain import keccak
    return "0x" + keccak(b"srnet-shop/v1" + bytes.fromhex(seller_key_hex)).hex()
