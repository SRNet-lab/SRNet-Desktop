"""Donation amounts and on-chain checks (read-only JSON-RPC, standard library only).

Each donation gets its own exact amount: what the donor asked for plus up to 0.01 USDC
(or up to 0.00001 ETH). No two open donations on the same chain and asset share an amount,
so a payment can be matched to its donation. A donation is marked paid only after its transaction is
found on the right chain, sends exactly that amount to the payout address, succeeded, has enough
confirmations, was mined within the donation's 7 days, and has not been used for another donation.
Nothing here holds a key or sends a transaction.
"""
from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

USDC_TOKENS = {   # as shown to donors (checksummed); compared in lower case
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",   # Arbitrum One (Circle)
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",    # Base (Circle)
}
# Two independent public RPCs per chain, both able to read week-old receipts (some free endpoints
# refuse anything older than a few hours).
CHAINS = {
    1: {"label": "Ethereum", "explorer": "https://etherscan.io/tx/", "confirmations": 12,
        "rpcs": ["https://eth.drpc.org", "https://mainnet.gateway.tenderly.co"]},
    42161: {"label": "Arbitrum One", "explorer": "https://arbiscan.io/tx/", "confirmations": 20,
            "rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org"]},
    8453: {"label": "Base", "explorer": "https://basescan.org/tx/", "confirmations": 10,
           "rpcs": ["https://mainnet.base.org", "https://base.drpc.org"]},
}
# (asset, chain) pairs offered. USDC only where Circle issues it natively and this page knows the token.
OPTIONS = [("eth", 1), ("eth", 42161), ("eth", 8453), ("usdc", 42161), ("usdc", 8453)]

ASSETS = {
    # decimals, how many decimals a donor may type, the step the matching tag uses, bounds
    "usdc": {"name": "USDC", "decimals": 6, "typed": 2, "tag_unit": 1, "min": "0.10", "max": "10000"},
    "eth": {"name": "ETH", "decimals": 18, "typed": 6, "tag_unit": 10**9, "min": "0.00001", "max": "5"},
}
TAG_MAX = 9999           # matching tag: 1..9999 steps of tag_unit added on top of the typed amount
OPEN_S = 7 * 86400        # an unpaid donation keeps its amount this long
EARLY_SLACK_S = 300       # a transaction mined up to this long before the donation opened still counts

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class AmountError(ValueError):
    pass


class Busy(AmountError):
    """The page can't open a new donation right now (limits or slots); donating still works."""


def offered(asset: str, chain: int) -> bool:
    return (asset, chain) in OPTIONS


def parse_amount(text: str, asset: str) -> int:
    """What the donor typed -> base units, within the asset's bounds."""
    a = ASSETS.get(asset)
    if not a:
        raise AmountError("unknown asset")
    t = str(text or "").strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,%d})?|\.[0-9]{1,%d}" % (a["typed"], a["typed"]), t):
        raise AmountError(f"type an amount like 1.50 (at most {a['typed']} decimals)")
    try:
        v = Decimal(t)
    except InvalidOperation:
        raise AmountError("not a number") from None
    if v < Decimal(a["min"]) or v > Decimal(a["max"]):
        raise AmountError(f"{a['name']} donations here are {a['min']} to {a['max']} {a['name']}")
    return int(v * (10 ** a["decimals"]))


def unique_units(asset: str, base: int, taken, rng=None) -> int:
    """base + a random tag (1..TAG_MAX steps) that no amount in `taken` already uses."""
    step = ASSETS[asset]["tag_unit"]
    pick = rng or secrets.randbelow
    taken = set(taken)
    for _ in range(64):
        u = base + (1 + pick(TAG_MAX)) * step
        if u not in taken:
            return u
    free = [base + i * step for i in range(1, TAG_MAX + 1) if base + i * step not in taken]
    if not free:
        raise Busy("too many open donations near this amount")
    return free[pick(len(free))]


def fmt_units(units: int, asset: str) -> str:
    d = ASSETS[asset]["decimals"]
    s = f"{Decimal(int(units)) / (Decimal(10) ** d):.{d}f}".rstrip("0")
    whole, _, frac = s.partition(".")
    return whole + "." + (frac + "00")[:max(2, len(frac))]


def pay_uri(asset: str, chain: int, to: str, units: int) -> str:
    """EIP-681 link most wallets open from a QR code."""
    if not ADDR_RE.match(to):
        raise AmountError("bad payout address")
    if asset == "eth":
        return f"ethereum:{to}@{chain}?value={int(units)}"
    return f"ethereum:{USDC_TOKENS[chain]}@{chain}/transfer?address={to}&uint256={int(units)}"


def any_uri(asset: str, chain: int, to: str) -> str:
    """EIP-681 link with the address (and token) but no amount: the wallet asks the donor how much."""
    if not ADDR_RE.match(to):
        raise AmountError("bad payout address")
    if asset == "eth":
        return f"ethereum:{to}@{chain}"
    return f"ethereum:{USDC_TOKENS[chain]}@{chain}/transfer?address={to}"


# ---- read-only JSON-RPC ----

class RpcError(Exception):
    pass


class JsonRpc:
    """Public RPCs often refuse requests without a User-Agent (Arbitrum's answers 403), so send one."""

    def __init__(self, url: str, timeout: float = 8, agent: str = "srnet-donate/1"):
        self.url, self.timeout, self.agent, self._id = url, timeout, agent, 0
        self.chain = None   # eth_chainId, once it has answered

    def call(self, method: str, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": self.agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read(4 << 20))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RpcError(f"{self.url}: {getattr(e, 'reason', e)}") from e
        if not isinstance(resp, dict) or resp.get("error"):
            raise RpcError(f"{self.url}: {method} failed")
        return resp.get("result")


def _int(v) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,64}", v):
        return int(v, 16)
    raise RpcError("bad number from RPC")


def _topic_addr(t: str) -> str:
    return "0x" + str(t)[-40:].lower()


TOPIC_ADDR_RE = re.compile(r"^0x0{24}[0-9a-f]{40}$")


def fetch_one(rpc, chain: int, tx: str) -> dict:
    """What one RPC says about a transaction: the tx, its receipt, the block at the receipt's height
    and the chain head. Raises RpcError if the RPC is on another chain or answers about another tx."""
    tx = tx.lower()
    if getattr(rpc, "chain", None) is None:
        got = _int(rpc.call("eth_chainId"))
        if hasattr(rpc, "chain"):
            rpc.chain = got
    else:
        got = rpc.chain
    if got != chain:
        raise RpcError("RPC is on another chain")
    t = rpc.call("eth_getTransactionByHash", tx)
    if not t:
        return {"tx": None}
    if str(t.get("hash", "")).lower() != tx:
        raise RpcError("RPC answered for another transaction")
    r = rpc.call("eth_getTransactionReceipt", tx)
    if not r or r.get("blockNumber") is None:
        return {"tx": t, "receipt": None}
    if str(r.get("transactionHash", "")).lower() != tx:
        raise RpcError("RPC sent the receipt of another transaction")
    blk = rpc.call("eth_getBlockByNumber", hex(_int(r["blockNumber"])), False)
    head = _int(rpc.call("eth_blockNumber"))
    return {"tx": t, "receipt": r, "block": blk, "head": head}


def judge(f: dict, d: dict, to: str) -> dict:
    """Does what one RPC said pay donation `d`? Returns {"state": paid|pending|bad, ...}."""
    chain, asset, want = int(d["chain"]), d["asset"], int(d["units"])
    t, r = f.get("tx"), f.get("receipt")
    if not t:
        return {"state": "pending", "why": f"transaction not found on {CHAINS[chain]['label']} (yet). "
                "If it was sent on another network, it won't show up here."}
    if t.get("chainId") is not None and _int(t["chainId"]) != chain:
        return {"state": "bad", "why": "that transaction is for another chain"}
    if not r:
        return {"state": "pending", "why": "sent but not mined yet"}
    bn, bh = _int(r["blockNumber"]), str(r.get("blockHash", "")).lower()
    if t.get("blockNumber") is None or _int(t["blockNumber"]) != bn:
        raise RpcError("transaction and receipt name different blocks")
    if _int(r.get("status", "0x0")) != 1:
        return {"state": "bad", "why": "that transaction failed"}
    to = to.lower()
    sender = str(t.get("from", "")).lower()
    if asset == "eth":
        if str(t.get("to") or "").lower() != to:
            return {"state": "bad", "why": "that transaction does not send ETH to the donation address "
                    "(payments forwarded by a smart-contract wallet can't be checked here)"}
        got = [_int(t.get("value", "0x0"))]
    else:
        token = USDC_TOKENS[chain].lower()
        got = []
        for lg in r.get("logs") or []:
            tp = [str(x).lower() for x in (lg.get("topics") or [])]
            if (str(lg.get("address", "")).lower() == token and len(tp) == 3 and tp[0] == TRANSFER_TOPIC
                    and TOPIC_ADDR_RE.match(tp[2]) and _topic_addr(tp[2]) == to):
                got.append(_int(lg.get("data") or "0x0"))
        if not got:
            return {"state": "bad", "why": "no USDC transfer to the donation address in that transaction"}
    if want not in got:
        low = max(got) < want
        return {"state": "bad", "why": ("underpaid" if low else "amount does not match") + ": got "
                + ", ".join(fmt_units(g, asset) for g in got) + f" {ASSETS[asset]['name']}, expected "
                + fmt_units(want, asset)}
    blk = f.get("block")
    if not blk or str(blk.get("hash", "")).lower() != bh:
        return {"state": "pending", "why": "the block holding it was replaced (reorg); check again shortly"}
    when = _int(blk["timestamp"])
    if when < int(d["created"]) - EARLY_SLACK_S:
        return {"state": "bad", "why": "that transaction was sent before this donation was opened"}
    if when > int(d["created"]) + OPEN_S:
        return {"state": "bad", "why": "that transaction was sent after this donation's 7 days were up"}
    conf = int(f["head"]) - bn + 1
    need = CHAINS[chain]["confirmations"]
    if conf < need:
        return {"state": "pending", "why": f"{max(conf, 0)} of {need} confirmations; check again shortly",
                "block": bn, "block_hash": bh}
    return {"state": "paid", "from": sender, "block": bn, "block_hash": bh, "time": when, "confirmations": conf}


def check_one(rpc, d: dict, to: str) -> dict:
    """Ask one RPC about donation `d`'s transaction."""
    return judge(fetch_one(rpc, int(d["chain"]), d["tx"]), d, to)


class TxCache:
    """What the RPCs said about a (chain, tx), kept briefly so repeated checks don't hit the RPCs."""

    def __init__(self, ttl: float = 30, size: int = 2000, clock=None):
        import threading
        import time
        self.ttl, self.size, self.clock = ttl, size, clock or time.time
        self.d: dict = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            hit = self.d.get(key)
            return hit[1] if hit and self.clock() - hit[0] < self.ttl else None

    def put(self, key, val):
        with self.lock:
            now = self.clock()
            if len(self.d) >= self.size:
                self.d = {k: v for k, v in self.d.items() if now - v[0] < self.ttl}
                while len(self.d) >= self.size:
                    self.d.pop(next(iter(self.d)))
            self.d[key] = (now, val)


def check(rpcs, d: dict, to: str, quorum: int | None = None, cache: TxCache | None = None) -> dict:
    """At least `quorum` RPCs (default: all of them) must answer, and every RPC that answers must
    agree, before a donation counts as paid. One RPC that lies or lags can delay a donation, never
    mark one paid on its own."""
    rpcs = list(rpcs)
    quorum = len(rpcs) if quorum is None else max(1, min(int(quorum), len(rpcs)))
    chain, tx = int(d["chain"]), str(d["tx"]).lower()
    facts = cache.get((chain, tx)) if cache else None
    if facts is None:
        def fetch(rpc):
            try:
                return fetch_one(rpc, chain, tx)
            except (RpcError, KeyError, TypeError, ValueError, AttributeError):
                return None

        if len(rpcs) > 1:   # ask them side by side so one slow RPC doesn't double the wait
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(len(rpcs)) as ex:
                facts = list(ex.map(fetch, rpcs))
        else:
            facts = [fetch(r) for r in rpcs]
        if cache:
            cache.put((chain, tx), facts)
    answers = []
    for f in facts:
        if f is None:
            continue
        try:
            answers.append(judge(f, d, to))
        except (RpcError, KeyError, TypeError, ValueError, AttributeError):
            pass
    if not answers:
        return {"state": "pending", "why": "couldn't reach the chain right now; check again shortly"}
    bad = [a for a in answers if a["state"] == "bad"]
    if bad:
        return bad[0]
    if all(a["state"] == "paid" for a in answers):
        if len(answers) < quorum:
            return {"state": "pending", "why": "only some of the chain checks answered; check again shortly"}
        if len({(a["block"], a["block_hash"]) for a in answers}) != 1:
            return {"state": "pending", "why": "the RPCs disagree about the block; check again shortly"}
        out = dict(answers[0])
        out["sources"] = len(answers)
        return out
    return next(a for a in answers if a["state"] == "pending")
