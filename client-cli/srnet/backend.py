"""lease_flow.Backend wiring: buy/claim -> map -> capable keeps the hour; failure unmaps and stops renewing.
Payment is final at start: there is no refund path, so a failed exit costs the prepaid hour."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LEASE = _HERE.parent / "vendor"
_PACK = _LEASE
for p in (_LEASE, _PACK):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from watcher import claim_message  # noqa: E402
from lease_flow import connect as lease_connect, ConnectResult  # noqa: E402

from .rpc import Rpc
from .state import State


class LiveBackend:
    """reserve = use pending buy (or error); map via Rpc; commit = keep the paid hour (no tx);
    release = no chain call (final payment); turn hourly auto-renew off so no further hour is bought."""

    def __init__(self, rpc: Rpc, chain, state: State, client_sesh: str, spool: Path,
                 wait_admission=None):
        self.rpc = rpc
        self.chain = chain
        self.state = state
        self.client_sesh = client_sesh
        self.spool = spool
        self.wait_admission = wait_admission  # optional callable(seller, timeout) -> bool

    def reserve(self, seller: str) -> str:
        pending = self.state.data.get("pending_buy")
        if not pending or pending.get("seller") != seller:
            raise RuntimeError("no pending buy for this seller; run: srnet buy <seller>")
        # (Re)drop claim so watcher can admit
        msg = claim_message(pending["contract"], pending["chainid"], pending["hold_id"], self.client_sesh)
        sig = self.chain.sign_claim(msg)
        claim = {"v": 1, "hold_id": pending["hold_id"], "client_sesh": self.client_sesh, "sig": sig}
        self.spool.mkdir(parents=True, exist_ok=True)
        (self.spool / f"hold-{pending['hold_id']}.json").write_text(json.dumps(claim))
        if self.wait_admission:
            if not self.wait_admission(seller, 60.0):
                raise RuntimeError("admission not observed")
        return str(pending["hold_id"])

    def map_exit(self, seller: str) -> None:
        r = self.rpc.map_exit(seller, full_tunnel=True)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r["error"])

    def wait_capable(self, seller: str, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self.rpc.list_exits()
            for e in (r.get("result") or []):
                if e.get("address") == seller and e.get("exit_capable") and e.get("session_live"):
                    return True
            time.sleep(2)
        return False

    def unmap_exit(self, seller: str) -> None:
        try:
            self.rpc.unmap_exit(seller)
        except Exception:
            pass

    def commit(self, hold_id: str) -> None:
        self.chain.settle_success(int(hold_id))
        self.state.data["pending_buy"] = None
        self.state.save()

    def release(self, hold_id: str) -> None:
        self.chain.release_failed(int(hold_id))  # no-op on chain
        self.state.data["pending_buy"] = None
        if self.state.data.get("lease"):
            self.state.data["lease"]["auto_renew"] = False
        self.state.save()


class MockBackend:
    """Fully offline backend for unit tests (wraps Fake from lease_flow tests style)."""

    def __init__(self, map_ok=True, capable=True, commit_ok=True, admit_ok=True):
        self.map_ok, self.capable, self.commit_ok, self.admit_ok = map_ok, capable, commit_ok, admit_ok
        self.log: list[str] = []
        self.spent = False
        self.mapped = False

    def reserve(self, seller: str) -> str:
        if not self.admit_ok:
            raise RuntimeError("no admission")
        self.log.append("reserve")
        return "1"

    def map_exit(self, seller: str) -> None:
        self.log.append("map")
        if not self.map_ok:
            raise RuntimeError("map refused")
        self.mapped = True

    def wait_capable(self, seller: str, timeout_s: float) -> bool:
        self.log.append("capable?")
        return self.capable

    def unmap_exit(self, seller: str) -> None:
        self.log.append("unmap")
        self.mapped = False

    def commit(self, hold_id: str) -> None:
        self.log.append("commit")
        if not self.commit_ok:
            raise RuntimeError("settle reverted")
        self.spent = True

    def release(self, hold_id: str) -> None:
        self.log.append("release")
