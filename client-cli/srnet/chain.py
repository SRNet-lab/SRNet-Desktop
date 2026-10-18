"""SRNetBalance helpers. Arbitrum One, Arbitrum Sepolia and test chains; anything else is refused.

Leases are prepaid whole hours and FINAL once started (no buyer cancel, no refund). The client buys
one hour and renews hour by hour (renewLease), so a failed or bad exit costs at most about one hour.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TESTNET_CHAINS = {31337: "anvil", 84532: "base-sepolia", 11155111: "sepolia", 421614: "arbitrum-sepolia"}
MAINNET_CHAINS = {42161: "arbitrum-one"}
CHAINS = {**MAINNET_CHAINS, **TESTNET_CHAINS}
# Known SRNetBalance deployments. On these chains a listing must point at this contract.
DEPLOYMENTS = {42161: "0x7E0d01B7dd916BdAB96B0971ab92D160E1664431"}  # fundWithPermit, 2026-10-14
HOUR = 3600
# Renew when the current lease has at most this many seconds left (the contract allows <= 1 hour).
RENEW_WINDOW = 600
USDC = 1_000_000  # base units per USDC

# Defaults: Arbitrum One and the live contract. Testnets are opt-in via --rpc / SRNET_RPC.
DEFAULT_CHAINID = 42161
DEFAULT_RPC = "https://arb1.arbitrum.io/rpc"
DEFAULT_CONTRACT = DEPLOYMENTS[DEFAULT_CHAINID]
# The SRNet demo relayer, on the demo node's .sesh name (reached through Session Router only).
# Used only with DEFAULT_CONTRACT and when no relayer is configured; "relayer": "off" disables it.
DEFAULT_RELAYER = "http://srnet7fqaajaqb6u7ho56znknysg5ime6kfbaymaaih4ba3q1ndo.sesh/relayer"
RELAYER_OFF = ("off", "none")


class ChainError(Exception):
    """A chain problem with a message meant for people, not stack traces."""


def _need_eth():
    try:
        import eth_abi  # noqa: F401
        import eth_account  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on the install
        raise ChainError("missing Python packages eth-account / eth-abi; run: pip install .") from e


def keccak(data: bytes) -> bytes:
    from eth_utils import keccak as _k
    return _k(data)


def selector(sig: str) -> bytes:
    return keccak(sig.encode())[:4]


def _types(sig: str) -> list[str]:
    inner = sig[sig.index("(") + 1: sig.rindex(")")]
    return [t for t in inner.split(",") if t]


def encode_call(sig: str, *args) -> bytes:
    """selector + ABI-encoded args for a function signature like 'fund(uint256)'."""
    from eth_abi import encode
    types = _types(sig)
    vals = []
    for t, a in zip(types, args):
        if t.startswith("bytes") and t != "bytes" and isinstance(a, str):
            a = bytes.fromhex(a.removeprefix("0x"))
        elif t.startswith("uint") or t.startswith("int"):
            a = int(a)
        vals.append(a)
    return selector(sig) + encode(types, vals)


def decode_result(types: list[str], data: bytes) -> tuple:
    from eth_abi import decode
    return tuple(decode(types, data))


# Custom errors from SRNetBalance (all take no arguments) -> plain-English text.
_ERRORS = {
    "ZeroAmount": "amount must be more than zero",
    "Paused": "the contract is paused right now; try later",
    "LeaseTooLarge": "lease is over the per-lease cap; pick fewer hours",
    "InsufficientAvailable": "not enough SRNet balance for this payment and relay fee; top up first",
    "VendorNotConfigured": "this seller has not registered its vendor id on the contract yet",
    "ZeroHours": "need at least one hour",
    "TooManyHours": "more hours than the contract allows",
    "ZeroRate": "listed price is zero",
    "RenewNotAllowed": "the seller stopped this lease; it can't be renewed",
    "RenewTooEarly": "too early to renew; renewals open in the last hour of a lease",
    "AlreadyRenewed": "this lease was already renewed; renew the newest lease instead",
    "NotLeaseBuyer": "this wallet didn't buy that lease",
    "UsdcCallFailed": "USDC transfer failed (check balance and allowance)",
    "UsdcReturnedFalse": "USDC transfer failed (check balance and allowance)",
    "TransferAmountMismatch": "USDC moved a different amount than asked",
    "LeaseNotOpen": "lease is not open",
    "InsufficientAllowance": "the USDC permit did not go through and there is no allowance; sign a new top-up",
}
for _n in ("ZeroAddress", "NotOwner", "NotPendingOwner", "NotLeaseSeller", "LeaseNotOver", "FeeBpsTooHigh",
           "InsufficientPayable", "VendorAlreadyConfigured", "NotVendorWallet", "SameWallet", "BadDeployCap",
           "BadToken", "CannotRescueUsdc", "Reentered", "EthRejected", "VendorIdMismatch", "FeeRecipientIsSelf",
           "NotCfVault", "BadVault", "RelayFeeTooHigh", "SignatureExpired", "NonceAlreadyUsed", "BadSignature",
           "WrongRelayer", "NothingHeld", "NativeSendFailed", "ExceedsUntracked"):
    _ERRORS.setdefault(_n, _n)
_ERROR_BY_SEL: dict | None = None


def explain_revert(data) -> str | None:
    """Map revert data (hex string) to a readable reason, or None."""
    global _ERROR_BY_SEL
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 10:
        return None
    if _ERROR_BY_SEL is None:
        _ERROR_BY_SEL = {selector(n + "()").hex(): n for n in _ERRORS}
    sel = data[2:10].lower()
    if sel in _ERROR_BY_SEL:
        n = _ERROR_BY_SEL[sel]
        return f"{_ERRORS[n]} ({n})" if _ERRORS[n] != n else n
    if sel == "08c379a0":  # Error(string)
        try:
            return decode_result(["string"], bytes.fromhex(data[10:]))[0]
        except Exception:
            return None
    if sel == "4e487b71":  # Panic(uint256): MockUSDC underflow etc.
        return "token transfer reverted (usually not enough USDC or allowance)"
    return None


def refuse_unsupported(chainid: int) -> None:
    if chainid not in CHAINS:
        names = ", ".join(f"{n} ({c})" for c, n in CHAINS.items())
        raise SystemExit(f"unsupported chain id {chainid}; supported: {names}")


refuse_mainnet = refuse_unsupported  # old name


class JsonRpc:
    """Minimal Ethereum JSON-RPC over HTTP(S)."""

    def __init__(self, url: str, timeout: float = 20):
        if not url:
            raise ChainError("no RPC URL; pass --rpc, set SRNET_RPC, or add \"rpc\" to config.json")
        self.url, self.timeout, self._id = url, timeout, 0

    def call(self, method: str, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": "srnet-exit"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ChainError(f"RPC {self.url} answered HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ChainError(f"can't reach RPC {self.url}: {getattr(e, 'reason', e)}") from e
        except json.JSONDecodeError as e:
            raise ChainError(f"RPC {self.url} did not return JSON") from e
        if "error" in resp and resp["error"]:
            err = resp["error"]
            data = err.get("data")
            if isinstance(data, dict):
                data = data.get("data") or data.get("result")
            why = explain_revert(data)
            raise ChainError(why or err.get("message", str(err)))
        return resp.get("result")


def build_tx(chainid: int, nonce: int, to: str, data: str, gas: int, tip: int, max_fee: int) -> dict:
    """EIP-1559 (type 2) transaction dict."""
    from eth_utils import to_checksum_address
    return {"type": 2, "chainId": int(chainid), "nonce": int(nonce), "to": to_checksum_address(to), "data": data,
            "value": 0, "gas": int(gas), "maxPriorityFeePerGas": int(tip), "maxFeePerGas": int(max_fee)}


def sign_tx(acct, tx: dict) -> str:
    """Signed raw transaction as 0x-hex."""
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    return "0x" + bytes(raw).hex()


@dataclass
class ChainCfg:
    rpc: str
    contract: str
    private_key: str  # buyer key (starts and renews its own leases; cannot cancel)
    receipt_timeout: float = 180
    relayer: str = ""  # relayer URL: when set, payments, renewals and cash-outs are gasless (no ETH)
    relay_fee: int = 20_000  # max USDC relay fee per gasless action (0.02)
    _rpc: JsonRpc = field(default=None, init=False, repr=False)  # type: ignore
    _acct: object = field(default=None, init=False, repr=False)
    _cid: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        _need_eth()
        self._rpc = JsonRpc(self.rpc)
        if not self.private_key:
            raise ChainError("no wallet key; run `srnet key new` or `srnet key import`")
        from eth_account import Account
        try:
            self._acct = Account.from_key(self.private_key)
        except Exception as e:
            raise ChainError("private key is not a valid 32-byte hex key") from e

    # -- reads
    def chainid(self) -> int:
        if self._cid is None:
            self._cid = int(self._rpc.call("eth_chainId"), 16)
        refuse_unsupported(self._cid)
        return self._cid

    def address(self) -> str:
        return self._acct.address

    def _call(self, sig: str, out: list[str], *args, to: str | None = None) -> tuple:
        res = self._rpc.call("eth_call", {"to": to or self.contract, "data": "0x" + encode_call(sig, *args).hex()},
                             "latest")
        raw = bytes.fromhex((res or "0x")[2:])
        if not raw and out:
            raise ChainError(f"no contract at {to or self.contract} on this chain")
        return decode_result(out, raw)

    def now(self) -> int:
        return int(self._rpc.call("eth_getBlockByNumber", "latest", False)["timestamp"], 16)

    def eth_balance(self) -> int:
        return int(self._rpc.call("eth_getBalance", self.address(), "latest"), 16)

    def usdc(self) -> str:
        return self._call("token()", ["address"])[0]

    def usdc_balance(self) -> int:
        return self._call("balanceOf(address)", ["uint256"], self.address(), to=self.usdc())[0]

    def allowance(self) -> int:
        return self._call("allowance(address,address)", ["uint256"], self.address(), self.contract, to=self.usdc())[0]

    def available(self) -> int:
        return self._call("availableOf(address)", ["uint256"], self.address())[0]

    def permit_info(self) -> tuple[int, str, str]:
        """(nonce, name, version) for the token's EIP-2612 domain."""
        t = self.usdc()
        nonce = self._call("nonces(address)", ["uint256"], self.address(), to=t)[0]
        name = self._call("name()", ["string"], to=t)[0]
        try:
            version = self._call("version()", ["string"], to=t)[0]
        except ChainError:
            version = "1"
        return nonce, name, version

    def fee_bps(self) -> int:
        return self._call("feeBps()", ["uint256"])[0]

    # -- writes
    def _send(self, sig: str, *args, to: str | None = None) -> dict:
        return self.send_data("0x" + encode_call(sig, *args).hex(), to=to)

    def send_data(self, data: str, to: str | None = None, max_gas: int | None = None) -> dict:
        from eth_utils import to_checksum_address
        to = to_checksum_address(to or self.contract)
        cid = self.chainid()
        frm = self.address()
        tx_call = {"from": frm, "to": to, "data": data}
        gas = int(self._rpc.call("eth_estimateGas", tx_call), 16)  # reverts surface here, decoded
        block = self._rpc.call("eth_getBlockByNumber", "latest", False)
        base = int(block.get("baseFeePerGas") or "0x0", 16)
        try:
            tip = int(self._rpc.call("eth_maxPriorityFeePerGas"), 16)
        except ChainError:
            tip = 10**9
        nonce = int(self._rpc.call("eth_getTransactionCount", frm, "pending"), 16)
        tx = build_tx(cid, nonce, to, data, gas * 12 // 10 + 10_000, tip, base * 2 + tip)
        if max_gas is not None and tx["gas"] * tx["maxFeePerGas"] > max_gas:
            raise ChainError(f"gas cost {tx['gas'] * tx['maxFeePerGas']} wei is over the allowed {max_gas} wei")
        need = tx["gas"] * tx["maxFeePerGas"]
        if self.eth_balance() < need:
            raise ChainError(f"wallet {frm} has no ETH for gas (about {need / 1e18:.6f} ETH needed); "
                             "turn the relayer back on (Wallet tab: clear the field and Save) so SRNet "
                             "payments need no ETH")
        h = self._rpc.call("eth_sendRawTransaction", sign_tx(self._acct, tx))
        return self._wait(h)

    def _wait(self, txh: str) -> dict:
        deadline = time.time() + self.receipt_timeout
        delay = 0.5
        while time.time() < deadline:
            rc = self._rpc.call("eth_getTransactionReceipt", txh)
            if rc:
                if int(rc.get("status", "0x1"), 16) != 1:
                    raise ChainError(f"transaction {txh} reverted")
                return rc
            time.sleep(delay)
            delay = min(delay * 1.5, 4)
        raise ChainError(f"transaction {txh} not mined after {int(self.receipt_timeout)}s; check it on an explorer")

    def approve(self, amount: int) -> None:
        self._send("approve(address,uint256)", self.contract, int(amount), to=self.usdc())

    def fund(self, usdc_amount: int) -> None:
        """Deposit USDC. Approves the contract first if the allowance is short."""
        usdc_amount = int(usdc_amount)
        bal = self.usdc_balance()
        if bal < usdc_amount:
            raise ChainError(f"not enough USDC: wallet has {bal / USDC:.2f}, deposit needs {usdc_amount / USDC:.2f}")
        if self.allowance() < usdc_amount:
            self.approve(usdc_amount)
        self._send("fund(uint256)", usdc_amount)

    def start_lease(self, vendor_id: str, rate_per_hour: int, hours: int = 1) -> int:
        """Prepay rate_per_hour x hours (whole hours >= 1; contract caps the total at $20).
        FINAL: the seller may take each hour the moment it begins; the buyer cannot cancel."""
        if int(hours) < 1 or int(rate_per_hour) < 1:
            raise ValueError("need rate_per_hour >= 1 and whole hours >= 1")
        need = int(rate_per_hour) * int(hours)
        have = self.available()
        if have < need:
            raise ChainError(f"SRNet balance is {have / USDC:.2f} USDC, lease needs {need / USDC:.2f}; "
                             "top up first")
        if self.relayer:
            if have < need + int(self.relay_fee):
                raise ChainError(f"SRNet balance is {have / USDC:.2f} USDC, payment plus relay fee needs "
                                 f"{(need + int(self.relay_fee)) / USDC:.2f}; top up first")
            return self._lease_id_from(self._relay("lease", {"vendorId": vendor_id, "ratePerHour": int(rate_per_hour),
                                                             "hoursBought": int(hours)}))
        rc = self._send("startLease(bytes32,uint256,uint256)", vendor_id, int(rate_per_hour), int(hours))
        return self._lease_id_from(rc)

    def renew(self, prev_lease_id: int, hours: int = 1) -> int:
        """Buy `hours` more from the same seller at the same rate, starting when prev ends."""
        if self.relayer:
            return self._lease_id_from(self._relay("renew", {"prevLeaseId": int(prev_lease_id),
                                                             "hoursBought": int(hours)}))
        rc = self._send("renewLease(uint256,uint256)", int(prev_lease_id), int(hours))
        return self._lease_id_from(rc)

    def renewed_by(self, lease_id: int) -> int:
        """Id of the lease that renewed lease_id, or 0. Each lease can be renewed once."""
        return int(self._call("renewedBy(uint256)", ["uint256"], int(lease_id))[0])

    def lease_window(self, lease_id: int) -> tuple:
        """(start, end, closed, seller_stopped)."""
        info = self._call("leaseInfo(uint256)",
                          ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256", "bool"],
                          int(lease_id))
        extra = self._call("leaseExtra(uint256)", ["bytes32", "bool", "uint256"], int(lease_id))
        start, dur = info[3], info[4]
        return start, start + dur, bool(info[7]), bool(extra[1])

    def _lease_id_from(self, rc: dict) -> int:
        # leaseId = first indexed topic of our LeaseStarted log; fall back to nextLeaseId.
        sig = "0x" + keccak(b"LeaseStarted(uint256,address,uint256,uint256,uint256)").hex()
        for log in rc.get("logs", []):
            t = log.get("topics") or []
            if t and t[0].lower() == sig and log.get("address", "").lower() == self.contract.lower():
                return int(t[1], 16)
        return self._call("nextLeaseId()", ["uint256"])[0]

    def open_hold(self, vendor_id: str, amount: int, duration: int = 3600) -> int:
        """Compat shim: amount for whole hours of duration -> startLease(rate=amount/hours, hours)."""
        hours = max(1, int(duration) // 3600)
        return self.start_lease(vendor_id, int(amount) // hours, hours)

    def hold_info(self, hold_id: int) -> tuple:
        """(buyer, amount, sellerWallet, end, active); active = open and before its end."""
        r = self._call("holdInfo(uint256)", ["address", "uint256", "address", "uint256", "bool"], int(hold_id))
        return r[0], int(r[1]), r[2], int(r[3]), bool(r[4])

    def lease_terms(self, lease_id: int) -> tuple:
        """(ratePerHour, hoursBought)."""
        return tuple(int(x) for x in self._call("leaseTerms(uint256)", ["uint256", "uint256"], int(lease_id)))

    def earned(self, lease_id: int) -> int:
        return int(self._call("earned(uint256)", ["uint256"], int(lease_id))[0])

    def settle_success(self, hold_id: int) -> None:
        """Nothing to do on success; the prepaid hour is the seller's."""
        return

    def release_failed(self, hold_id: int) -> None:
        """Exit did not work -> no chain call exists (payment is final). The caller unmaps and
        stops renewing; the loss is the prepaid hour(s) of this lease."""
        return

    def cash_out(self, amount: int, to: str | None = None) -> None:
        """Withdraw unused SRNet balance to `to` (default: this wallet). Gasless when a relayer is set."""
        if self.relayer:
            from eth_utils import to_checksum_address
            self._relay("withdraw", {"amount": int(amount), "to": to_checksum_address(to or self.address())})
            return
        self._send("cashOut(uint256)", int(amount))

    def _relay(self, kind: str, fields: dict) -> dict:
        """Sign an EIP-712 intent, hand it to the relayer (it pays gas), return the mined receipt."""
        from . import gasless
        req = gasless.sign_action(self._acct, kind, fields, chainid=self.chainid(), contract=self.contract,
                                  relay_fee=int(self.relay_fee),
                                  deadline=max(int(time.time()), self.now()) + gasless.INTENT_TTL)
        out = gasless.submit_action(self.relayer, req)
        if not out.get("tx"):
            raise ChainError(f"relayer returned no transaction: {str(out)[:200]}")
        return self._wait(out["tx"])

    def sign_claim(self, message: str) -> str:
        """EIP-191 personal_sign, same bytes as `cast wallet sign`."""
        from eth_account.messages import encode_defunct
        sig = self._acct.sign_message(encode_defunct(text=message)).signature
        return "0x" + bytes(sig).hex()


@dataclass
class MockChain:
    """Offline stand-in for unit tests."""
    chainid: int = 31337
    contract: str = "0x" + "c0" * 20
    user: str = "0x" + "11" * 20
    holds: dict = None  # type: ignore
    next_id: int = 1
    settled: set = None  # type: ignore
    released: set = None  # type: ignore

    def __post_init__(self):
        self.holds = {} if self.holds is None else self.holds
        self.settled = set() if self.settled is None else self.settled
        self.released = set() if self.released is None else self.released

    def address(self) -> str:
        return self.user

    clock: int = 1_000_000

    def start_lease(self, vendor_id: str, rate_per_hour: int, hours: int = 1, start: int | None = None) -> int:
        if int(hours) < 1 or int(rate_per_hour) < 1:
            raise ValueError("need rate_per_hour >= 1 and whole hours >= 1")
        if int(rate_per_hour) * int(hours) > 20_000_000:
            raise ValueError("LeaseTooLarge: over $20 per lease")
        hid = self.next_id
        self.next_id += 1
        st = self.clock if start is None else start
        self.holds[hid] = {"vendor_id": vendor_id, "amount": int(rate_per_hour) * int(hours), "active": True,
                           "wallet": "0x" + "aa" * 20, "rate": int(rate_per_hour), "hours": int(hours),
                           "start": st, "end": st + int(hours) * HOUR, "stopped": False}
        return hid

    def renewed_by(self, lease_id: int) -> int:
        return self.holds.get(lease_id, {}).get("renewed_by", 0)

    def renew(self, prev_lease_id: int, hours: int = 1) -> int:
        p = self.holds[prev_lease_id]
        if p.get("renewed_by"):
            raise RuntimeError("AlreadyRenewed")
        if p.get("stopped"):
            raise RuntimeError("RenewNotAllowed")
        if p["end"] > self.clock + HOUR:
            raise RuntimeError("RenewTooEarly")
        new = self.start_lease(p["vendor_id"], p["rate"], hours, start=max(self.clock, p["end"]))
        p["renewed_by"] = new
        return new

    def lease_window(self, lease_id: int) -> tuple:
        h = self.holds[lease_id]
        closed = (not h["active"]) or self.clock >= h["end"]
        return h["start"], h["end"], closed, h.get("stopped", False)

    def now(self) -> int:
        return self.clock

    def open_hold(self, vendor_id: str, amount: int, duration: int = 3600) -> int:
        hours = max(1, int(duration) // 3600)
        return self.start_lease(vendor_id, max(1, int(amount) // hours), hours)

    def fund(self, usdc_amount: int) -> None:
        return

    def seller_cancel(self, hold_id: int) -> None:
        """Test helper: the SELLER stops the lease (the buyer has no cancel)."""
        self.holds[hold_id]["active"] = False
        self.holds[hold_id]["stopped"] = True

    def settle_success(self, hold_id: int) -> None:
        self.settled.add(hold_id)  # no chain call; recorded for tests only

    def release_failed(self, hold_id: int) -> None:
        self.released.add(hold_id)  # no chain call (payment final); recorded for tests only

    def sign_claim(self, message: str) -> str:
        return f"mock:{self.user.lower()}:{message}"

    def hold_info(self, hold_id: int):
        h = self.holds.get(hold_id)
        if not h:
            return "0x" + "00" * 20, 0, "0x" + "00" * 20, 0, False
        return self.user, h["amount"], h["wallet"], h.get("end", 2**63), h["active"] and self.clock < h.get("end", 2**63)


def newest_lease(ch, lease_id: int, limit: int = 10_000) -> int:
    """Follow renewedBy from lease_id to the end of the chain. A renewal whose result was never
    saved locally (crash, lost receipt, a second client) is picked up here instead of retried."""
    cur = int(lease_id)
    for _ in range(limit):
        nxt = int(ch.renewed_by(cur)) if hasattr(ch, "renewed_by") else 0
        if not nxt:
            return cur
        cur = nxt
    raise ChainError("renewal chain too long")


def renew_due(ch, lease_id: int, window: int = RENEW_WINDOW) -> tuple[bool, str]:
    """Hourly auto-renew policy: renew when the lease has <= `window` seconds left, is not stopped by the
    seller, and has not already been renewed (caller tracks that). Returns (due, reason)."""
    start, end, closed, stopped = ch.lease_window(lease_id)
    now = ch.now()
    if stopped:
        return False, "seller_stopped"
    if end <= now:
        return True, "ended"  # renewal starts now (gap)
    if end - now <= window:
        return True, "in_window"
    return False, f"wait:{end - now - window}"


def read_only(rpc: str, contract: str) -> "ChainCfg":
    """A ChainCfg for eth_call reads only. Holds a random throwaway key that never signs anything."""
    import secrets
    return ChainCfg(rpc=rpc, contract=contract, private_key="0x" + secrets.token_hex(32))


def relayer_url(cfg: dict | None = None, contract: str | None = None) -> str:
    """The relayer used for gasless top-ups, payments, renewals and cash-outs.

    "relayer" in config.json, then $SRNET_RELAYER, then DEFAULT_RELAYER when the contract is the
    default one. "off" (or "none") in either place means no relayer."""
    import os
    cfg = cfg or {}
    v = cfg.get("relayer")
    if v is None or not str(v).strip():
        v = os.environ.get("SRNET_RELAYER")
    v = (v or "").strip()
    if v.lower() in RELAYER_OFF:
        return ""
    if v:
        return v
    c = (contract or cfg.get("contract") or DEFAULT_CONTRACT).strip().lower()
    return DEFAULT_RELAYER if c == DEFAULT_CONTRACT.lower() else ""
