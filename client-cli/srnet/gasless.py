"""Gasless top-up and gasless SRNet-balance actions (no ETH needed).

1. The client shows a QR (EIP-681 plain USDC transfer) of the user's OWN SRNet wallet address.
2. The user sends USDC from any phone or exchange wallet. No ETH is needed anywhere.
3. When the USDC arrives, the client signs an EIP-2612 permit (USDC -> SRNetBalance) and an EIP-712
   Fund intent, and hands both to a relayer. The relayer calls SRNetBalance.fundWithPermit and is
   paid `relay_fee` USDC out of the deposit (capped on-chain by maxRelayFee).

The intent binds owner, beneficiary, amount, relay fee, relayer, nonce and deadline, so a relayer
cannot redirect funds, change the amount or charge more than signed.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request

from . import chain

# Circle native USDC (FiatTokenV2_2, supports EIP-2612; permit domain name "USD Coin", version "2").
USDC_BY_CHAIN = {
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    421614: "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",
}
DEFAULT_RELAY_FEE = 50_000  # 0.05 USDC default (measured gas per relayed top-up is about $0.01)
INTENT_TTL = 1800

TOPUP_WARNING = """\
This QR funds YOUR SRNet wallet ({address}) with USDC on {chain_name}.
Send only USDC on {chain_name}. Other tokens or networks will not arrive.
You do not need ETH. When the USDC lands, this client asks a relayer to move it into
your SRNet balance. The deposit fee (up to 3%) and a small relayer fee ({relay_fee} USDC)
come out of the deposit and are not refunded on cash-out."""

FUND_TYPES = {
    "Fund": [
        {"name": "owner", "type": "address"},
        {"name": "beneficiary", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "relayFee", "type": "uint256"},
        {"name": "relayer", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]
}
PERMIT_TYPES = {
    "Permit": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]
}
FUND_SIG = ("fundWithPermit((address,address,uint256,uint256,address,uint256,uint256),bytes,"
            "(uint256,uint256,uint8,bytes32,bytes32))")
ZERO = "0x" + "00" * 20


def eip681_uri(usdc: str, to: str, chainid: int, amount: int | None = None) -> str:
    """EIP-681 ERC-20 transfer: ethereum:<token>@<chain>/transfer?address=<to>[&uint256=<amount>]."""
    from eth_utils import to_checksum_address as cs
    uri = f"ethereum:{cs(usdc)}@{chainid}/transfer?address={cs(to)}"
    if amount:
        uri += f"&uint256={int(amount)}"
    return uri


def qr_text(data: str) -> str:
    """Terminal QR if the optional `qrcode` package is installed, else the URI itself."""
    try:
        import io
        import qrcode  # type: ignore
        q = qrcode.QRCode(border=1)
        q.add_data(data)
        buf = io.StringIO()
        q.print_ascii(out=buf, invert=True)
        return buf.getvalue()
    except ImportError:
        return data


def topup_screen(address: str, chainid: int, usdc: str, relay_fee: int = DEFAULT_RELAY_FEE,
                 amount: int | None = None) -> str:
    uri = eip681_uri(usdc, address, chainid, amount)
    warn = TOPUP_WARNING.format(address=address, chain_name=chain.CHAINS.get(chainid, chainid),
                                relay_fee=f"{relay_fee / chain.USDC:.2f}")
    qr = qr_text(uri)
    return f"{warn}\n\n{qr}\n{uri}\n" if qr != uri else f"{warn}\n\n{uri}\n"


def sign_topup(acct, *, chainid: int, contract: str, usdc: str, amount: int, permit_nonce: int,
               relay_fee: int = DEFAULT_RELAY_FEE, relayer: str = ZERO, beneficiary: str | None = None,
               nonce: int | None = None, deadline: int | None = None,
               usdc_name: str = "USD Coin", usdc_version: str = "2") -> dict:
    """Sign permit + Fund intent. Returns the JSON request a relayer accepts."""
    from eth_utils import to_checksum_address as cs
    deadline = int(deadline or time.time() + INTENT_TTL)
    nonce = int(nonce if nonce is not None else secrets.randbits(128))
    owner = acct.address
    intent = {"owner": owner, "beneficiary": cs(beneficiary or owner), "amount": int(amount),
              "relayFee": int(relay_fee), "relayer": cs(relayer), "nonce": nonce, "deadline": deadline}
    s1 = acct.sign_typed_data(
        {"name": "SRNetBalance", "version": "1", "chainId": chainid, "verifyingContract": cs(contract)},
        FUND_TYPES, intent)
    permit_msg = {"owner": owner, "spender": cs(contract), "value": int(amount), "nonce": int(permit_nonce),
                  "deadline": deadline}
    s2 = acct.sign_typed_data(
        {"name": usdc_name, "version": usdc_version, "chainId": chainid, "verifyingContract": cs(usdc)},
        PERMIT_TYPES, permit_msg)
    return {
        "chainid": chainid, "contract": cs(contract), "intent": intent,
        "sig": "0x" + bytes(s1.signature).hex(),
        "permit": {"value": int(amount), "deadline": deadline, "v": s2.v,
                   "r": "0x%064x" % s2.r, "s": "0x%064x" % s2.s},
    }


def encode_fund_call(req: dict) -> str:
    from eth_abi import encode
    i, p = req["intent"], req["permit"]
    args = encode(
        ["(address,address,uint256,uint256,address,uint256,uint256)", "bytes", "(uint256,uint256,uint8,bytes32,bytes32)"],
        [(i["owner"], i["beneficiary"], int(i["amount"]), int(i["relayFee"]), i["relayer"], int(i["nonce"]),
          int(i["deadline"])),
         bytes.fromhex(req["sig"][2:]),
         (int(p["value"]), int(p["deadline"]), int(p["v"]), bytes.fromhex(p["r"][2:]), bytes.fromhex(p["s"][2:]))])
    return "0x" + (chain.selector(FUND_SIG) + args).hex()


def wait_for_usdc(read_balance, minimum: int, timeout: float = 1800, poll: float = 5, sleep=time.sleep) -> int:
    """Poll the wallet's USDC balance until it reaches `minimum`. Returns the balance."""
    end = time.time() + timeout
    while True:
        bal = int(read_balance())
        if bal >= minimum:
            return bal
        if time.time() >= end:
            raise chain.ChainError(f"no USDC arrived after {int(timeout)}s (have {bal / chain.USDC:.2f})")
        sleep(poll)


def relayer_info(relayer_url: str, timeout: float = 20) -> dict:
    """GET the relayer's identity: {ok, relayer, chainid, contract}. Raises ChainError if unreachable."""
    try:
        with urllib.request.urlopen(relayer_url.rstrip("/") + "/", timeout=timeout) as resp:
            d = json.loads(resp.read())
    except (OSError, ValueError) as e:
        raise chain.ChainError(f"relayer not reachable: {e}") from e
    if not isinstance(d, dict) or not d.get("ok"):
        raise chain.ChainError(f"relayer not ready: {str(d)[:200]}")
    return d


def submit(relayer_url: str, req: dict, timeout: float = 60) -> dict:
    body = json.dumps(req).encode()
    r = urllib.request.Request(relayer_url.rstrip("/") + "/fund", data=body,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise chain.ChainError(f"relayer refused: {e.read().decode(errors='replace')[:300]}") from e


# ---- gasless SRNet-balance actions: pay (startLeaseFor), renew (renewFor), cash out (withdrawFor) ----
# The user signs an EIP-712 intent; the relayer submits it and pays the gas. The relay fee (USDC) comes
# out of the user's SRNet balance on top of the action, capped on-chain by maxRelayFee.
ACTION_RELAY_FEE = 20_000  # 0.02 USDC default per payment / renewal / cash-out

ACTION_TYPES = {
    "lease": {"StartLease": [
        {"name": "buyer", "type": "address"}, {"name": "vendorId", "type": "bytes32"},
        {"name": "ratePerHour", "type": "uint256"}, {"name": "hoursBought", "type": "uint256"},
        {"name": "relayFee", "type": "uint256"}, {"name": "relayer", "type": "address"},
        {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"}]},
    "renew": {"Renew": [
        {"name": "buyer", "type": "address"}, {"name": "prevLeaseId", "type": "uint256"},
        {"name": "hoursBought", "type": "uint256"}, {"name": "relayFee", "type": "uint256"},
        {"name": "relayer", "type": "address"}, {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"}]},
    "withdraw": {"Withdraw": [
        {"name": "user", "type": "address"}, {"name": "amount", "type": "uint256"},
        {"name": "to", "type": "address"}, {"name": "relayFee", "type": "uint256"},
        {"name": "relayer", "type": "address"}, {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"}]},
}
ACTION_SIGS = {
    "lease": "startLeaseFor((address,bytes32,uint256,uint256,uint256,address,uint256,uint256),bytes)",
    "renew": "renewFor((address,uint256,uint256,uint256,address,uint256,uint256),bytes)",
    "withdraw": "withdrawFor((address,uint256,address,uint256,address,uint256,uint256),bytes)",
}
ACTION_TUPLES = {
    "lease": "(address,bytes32,uint256,uint256,uint256,address,uint256,uint256)",
    "renew": "(address,uint256,uint256,uint256,address,uint256,uint256)",
    "withdraw": "(address,uint256,address,uint256,address,uint256,uint256)",
}


def sign_action(acct, kind: str, fields: dict, *, chainid: int, contract: str,
                relay_fee: int = ACTION_RELAY_FEE, relayer: str = ZERO,
                nonce: int | None = None, deadline: int | None = None) -> dict:
    """Sign a StartLease / Renew / Withdraw intent. Returns the JSON request a relayer accepts."""
    from eth_utils import to_checksum_address as cs
    if kind not in ACTION_TYPES:
        raise ValueError(f"unknown action {kind!r}")
    signer_field = "user" if kind == "withdraw" else "buyer"
    msg = {signer_field: acct.address, **fields, "relayFee": int(relay_fee), "relayer": cs(relayer),
           "nonce": int(nonce if nonce is not None else secrets.randbits(128)),
           "deadline": int(deadline or time.time() + INTENT_TTL)}
    if kind == "lease" and isinstance(msg["vendorId"], str):
        msg["vendorId"] = bytes.fromhex(msg["vendorId"].removeprefix("0x"))
    types = ACTION_TYPES[kind]
    order = [f["name"] for f in next(iter(types.values()))]
    s = acct.sign_typed_data(
        {"name": "SRNetBalance", "version": "1", "chainId": chainid, "verifyingContract": cs(contract)},
        types, msg)
    out = {k: ("0x" + msg[k].hex() if isinstance(msg[k], (bytes, bytearray)) else msg[k]) for k in order}
    return {"kind": kind, "chainid": chainid, "contract": cs(contract), "intent": out,
            "sig": "0x" + bytes(s.signature).hex()}


def encode_action_call(req: dict) -> str:
    from eth_abi import encode
    kind, i = req["kind"], req["intent"]
    order = [f["name"] for f in next(iter(ACTION_TYPES[kind].values()))]
    vals = []
    for k in order:
        v = i[k]
        if k == "vendorId":
            v = bytes.fromhex(str(v).removeprefix("0x"))
        elif k in ("buyer", "user", "to", "relayer"):
            v = str(v)
        else:
            v = int(v)
        vals.append(v)
    args = encode([ACTION_TUPLES[kind], "bytes"], [tuple(vals), bytes.fromhex(req["sig"][2:])])
    return "0x" + (chain.selector(ACTION_SIGS[kind]) + args).hex()


def submit_action(relayer_url: str, req: dict, timeout: float = 60) -> dict:
    body = json.dumps(req).encode()
    r = urllib.request.Request(relayer_url.rstrip("/") + "/" + req["kind"], data=body,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise chain.ChainError(f"relayer refused: {e.read().decode(errors='replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise chain.ChainError(f"relayer unreachable at {relayer_url}: {e.reason}") from e
