"""Mirror: verifies everything with the same code the client uses (root pin -> delegation ->
checkpoint -> chain -> post and record signatures) and re-serves the primary's own signed
checkpoint.  It cannot sign anything.  Gets data by push (/v1/publish from the primary) and by
pull (periodic, fallback).  Posts and deletes submitted to a mirror are forwarded to the
primary; if it is down the mirror answers 503 and the client keeps the post in its outbox."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time

import mbfmt as F
from .sync import SyncError, accept_batch, submit, sync_once
from .record import BOARD
from .node import serve, url_of
from .store import Store


class Mirror:
    def __init__(self, data_dir: str, board: str, root_pin: str, upstreams: list[str],
                 clock=time.time, codec=BOARD):
        self.codec = codec
        os.makedirs(data_dir, exist_ok=True)
        self.board, self.root_pin, self.upstreams, self.clock = board, root_pin, upstreams, clock
        self.store = Store(os.path.join(data_dir, "log.sqlite3"))
        self.pin = F.CheckpointPin(*self.store.head())
        self.state_path = os.path.join(data_dir, "mirror-state.json")
        self._cp = self.delegation = self.policy = None
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                s = json.load(f)
            self._cp, self.delegation, self.policy = s["checkpoint"], s["delegation"], s["policy"]
        self.lock = threading.Lock()
        self.last = {}

    def _keep(self, cp, d, pol):
        self._cp, self.delegation, self.policy = cp, d, pol
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"checkpoint": cp, "delegation": d, "policy": pol}, f)
        os.replace(tmp, self.state_path)

    def publish(self, p: dict):
        with self.lock:
            try:
                ents, recs = list(p.get("entries", [])), list(p.get("records", []))
                r = accept_batch(self.store, self.pin, self.root_pin, self.board,
                                 p.get("delegation"), p.get("checkpoint"),
                                 lambda s: [e for e in ents if e.get("seq", 0) > s],
                                 lambda s: [x for x in recs if x.get("rseq", 0) > s],
                                 policy=p.get("policy"), now=self.clock(), codec=self.codec)
            except (SyncError, F.FormatError, KeyError, TypeError, ValueError,
                    AttributeError) as e:
                return 400, {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._keep(p["checkpoint"], p["delegation"], p["policy"])
            self.last = {"via": "push", "at": int(self.clock()), **{k: r[k] for k in
                         ("added", "records", "seq")}}
            return 200, {"ok": True, **{k: r[k] for k in ("added", "records", "seq")}}

    def pull(self) -> dict:
        with self.lock:
            r = sync_once(self.store, self.upstreams, self.root_pin, self.board, self.pin,
                          now=self.clock(), codec=self.codec)
            self._keep(r["checkpoint"], r["delegation"], r["policy"])
            self.last = {"via": "pull", "at": int(self.clock()), "added": r["added"]}
            return r

    def checkpoint(self):
        return self._cp

    def info(self):
        seq, _ = self.store.head()
        i = {"v": 1, "board": self.board, "role": "mirror", "seq": seq,
             "rseq": self.store.rhead(), "last": self.last}
        if self.policy:
            i["policy"] = F.policy_hash(self.policy)
            i.update(pow_bits=self.policy["pow_bits"],
                     new_key_pow_bits=self.policy["new_key_pow_bits"])
        return i

    def _forward(self, env, path):
        r = submit(self.upstreams, env, path)
        if r.get("ok"):
            try:
                self.pull()
            except SyncError:
                pass
            return 200, dict(r, forwarded=True)
        if r.get("unreachable"):
            return 503, {"ok": False, "error": "primary unreachable; try again later"}
        return r.get("status", 400), r

    def submit(self, env):
        return self._forward(env, "/v1/submit")

    def delete(self, env):
        return self._forward(env, "/v1/delete")

    def run_puller(self, every: float = 60.0):
        def loop():
            while True:
                try:
                    self.pull()
                except SyncError:
                    pass
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="srnet-msgboard mirror")
    ap.add_argument("--data", required=True)
    ap.add_argument("--board", default="demo")
    ap.add_argument("--root-pin", required=True)
    ap.add_argument("--upstream", action="append", required=True)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48801)
    ap.add_argument("--pull-every", type=float, default=60.0)
    a = ap.parse_args()
    m = Mirror(a.data, a.board, a.root_pin, a.upstream)
    srv = serve(m, a.bind, a.port)
    m.run_puller(a.pull_every)
    print(json.dumps({"url": url_of(srv), "role": "mirror", "upstreams": a.upstream}), flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
