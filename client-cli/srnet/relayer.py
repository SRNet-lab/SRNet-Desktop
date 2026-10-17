"""Minimal SRNet relayer: pays the gas so users never need ETH. Anyone may run one.

POST /fund      {chainid, contract, intent, sig, permit}  top-up (gasless.sign_topup -> fundWithPermit)
POST /lease     {kind, chainid, contract, intent, sig}     pay a seller (gasless.sign_action -> startLeaseFor)
POST /renew     {kind, chainid, contract, intent, sig}     next hour (renewFor)
POST /withdraw  {kind, chainid, contract, intent, sig}     cash out (withdrawFor)
GET  /          {ok, relayer, chainid, contract}           health / identity

Paths may carry a prefix (e.g. /relayer/fund behind a path router on a .sesh name).

It holds only its own gas key and its earned relay fees (credited inside SRNetBalance to
payableOf[relayer]; withdraw with withdrawPayable). It never holds user USDC.

Guards: per-IP and per-owner rate limits, minimum fee, contract allow-list, simulation via
eth_estimateGas before sending, per-tx gas cap and a rolling 24h gas budget in wei.
Behind a local reverse proxy (--trust-proxy), the per-IP limit uses the X-Forwarded-For address the
proxy sets, but only for connections from loopback.

  SRNET_RELAYER_KEY=0x... SRNET_RPC=... python -m srnet.relayer --contract 0x... [--port 8645]
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import chain, gasless


class RateLimiter:
    def __init__(self, limit: int, window: float, clock=time.time):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = self.clock(), self.hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class GasBudget:
    def __init__(self, wei_per_day: int, clock=time.time):
        self.cap, self.clock, self.spent = wei_per_day, clock, deque()

    def remaining(self) -> int:
        now = self.clock()
        while self.spent and self.spent[0][0] <= now - 86400:
            self.spent.popleft()
        return self.cap - sum(w for _, w in self.spent)

    def charge(self, wei: int) -> None:
        self.spent.append((self.clock(), int(wei)))


class Relayer:
    def __init__(self, ch, *, min_fee: int = 50_000, min_action_fee: int = 20_000, max_gas_per_tx: int = 2 * 10**14,
                 gas_budget_wei: int = 5 * 10**15, ip_limit=(10, 3600), owner_limit=(5, 3600), clock=time.time):
        self.ch, self.min_fee, self.max_gas_per_tx = ch, min_fee, max_gas_per_tx
        self.min_action_fee = min_action_fee
        self.budget = GasBudget(gas_budget_wei, clock)
        self.by_ip, self.by_owner = RateLimiter(*ip_limit, clock), RateLimiter(*owner_limit, clock)
        self.lock = threading.Lock()

    def handle(self, req: dict, ip: str = "?", kind: str = "fund") -> tuple[int, dict]:
        try:
            i = req["intent"]
            if kind == "fund":
                owner, min_fee = i["owner"], self.min_fee
            elif kind in gasless.ACTION_TYPES and req.get("kind") == kind:
                owner, min_fee = i["user" if kind == "withdraw" else "buyer"], self.min_action_fee
            else:
                return 400, {"error": "unknown action"}
            if int(req.get("chainid", -1)) != self.ch.chainid():
                return 400, {"error": "wrong chain"}
            if req.get("contract", "").lower() != self.ch.contract.lower():
                return 400, {"error": "unknown contract"}
            if int(i["relayFee"]) < min_fee:
                return 400, {"error": f"relay fee below this relayer's minimum {min_fee}"}
            rel = i["relayer"].lower()
            if rel not in (gasless.ZERO, self.ch.address().lower()):
                return 400, {"error": "intent is pinned to another relayer"}
            if int(i["deadline"]) < time.time() + 30:
                return 400, {"error": "intent expires too soon"}
            data = gasless.encode_fund_call(req) if kind == "fund" else gasless.encode_action_call(req)
        except (KeyError, ValueError, TypeError) as e:
            return 400, {"error": f"bad request: {e}"}
        if not self.by_ip.allow(ip) or not self.by_owner.allow(owner.lower()):
            return 429, {"error": "rate limited"}
        with self.lock:
            cap = min(self.max_gas_per_tx, self.budget.remaining())
            if cap <= 0:
                return 503, {"error": "relayer gas budget used up for today"}
            try:
                rc = self.ch.send_data(data, max_gas=cap)  # estimateGas simulates; reverts never get sent
            except chain.ChainError as e:
                return 422, {"error": str(e)}
            used = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
            self.budget.charge(used)
        return 200, {"ok": True, "tx": rc["transactionHash"], "gas_wei": used}


def _kind(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def serve(relayer: Relayer, host: str = "127.0.0.1", port: int = 8645,
          trust_proxy: bool = False) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _ip(self) -> str:
            ip = self.client_address[0]
            fwd = self.headers.get("X-Forwarded-For")
            if trust_proxy and fwd and ip in LOOPBACK:
                return fwd.split(",")[-1].strip()[:64] or ip
            return ip

        def do_GET(self):
            if _kind(self.path) not in ("", "relayer", "health"):
                return self._out(404, {"error": "not found"})
            try:
                info = {"ok": True, "relayer": relayer.ch.address(), "chainid": relayer.ch.chainid(),
                        "contract": relayer.ch.contract}
            except Exception as e:  # noqa: BLE001 - report, never crash the handler
                return self._out(503, {"ok": False, "error": f"chain unreachable: {e}"[:200]})
            self._out(200, info)

        def do_POST(self):
            kind = _kind(self.path)
            if kind not in ("fund", *gasless.ACTION_TYPES):
                return self._out(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8192:
                return self._out(413, {"error": "too large"})
            try:
                req = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                return self._out(400, {"error": "bad json"})
            self._out(*relayer.handle(req, self._ip(), kind))

        def _out(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="srnet-relayer")
    ap.add_argument("--contract", required=True)
    ap.add_argument("--rpc", default=os.environ.get("SRNET_RPC"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8645)
    ap.add_argument("--min-fee", type=int, default=50_000, help="USDC base units (default 0.05)")
    ap.add_argument("--min-action-fee", type=int, default=20_000,
                    help="USDC base units for pay/renew/cash-out (default 0.02)")
    ap.add_argument("--max-gas-per-tx-wei", type=int, default=2 * 10**14, help="refuse a relay costing more")
    ap.add_argument("--gas-budget-wei", type=int, default=5 * 10**15, help="per rolling 24h")
    ap.add_argument("--trust-proxy", action="store_true",
                    help="rate-limit by X-Forwarded-For from a loopback reverse proxy (srnet-front)")
    a = ap.parse_args(argv)
    key = os.environ.get("SRNET_RELAYER_KEY")
    if not key:
        raise SystemExit("set SRNET_RELAYER_KEY to the relayer's own gas key (not a user or owner key)")
    ch = chain.ChainCfg(rpc=a.rpc, contract=a.contract, private_key=key)
    if ch.chainid() in chain.MAINNET_CHAINS and os.environ.get("SRNET_RELAYER_MAINNET") != "1":
        raise SystemExit("refusing mainnet without SRNET_RELAYER_MAINNET=1")
    serve(Relayer(ch, min_fee=a.min_fee, min_action_fee=a.min_action_fee, max_gas_per_tx=a.max_gas_per_tx_wei, gas_budget_wei=a.gas_budget_wei), a.host, a.port,
          trust_proxy=a.trust_proxy)
    print(f"relayer {ch.address()} on {a.host}:{a.port} for {a.contract}")
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
