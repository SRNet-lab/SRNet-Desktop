"""Fresh-tester connect paths: no --client-sesh, reconnect inside the paid lease after
disconnect, the kill-switch defaults, and the .sesh DNS default."""
import io as _io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.stdin = _io.StringIO()
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from srnet import cli, killswitch, state  # noqa: E402

SELLER = "y3yi9753qbe4gxr31t1urywd3yd9pk4a157g7jeg9xsz6iecu73y.sesh"
ME = "xykbqrmeoadf48m34nfay44d1esquuz6y1efudifhhm4jc6xbs3o.sesh"
CONTRACT = "0x" + "c0" * 20


class FakeChain:
    """The read/sign surface connect uses on a live ChainCfg."""
    def __init__(self, leases, now=10_000):
        self.leases, self.clock, self.contract, self.claims = leases, now, CONTRACT, []

    def chainid(self):
        return 42161

    def now(self):
        return self.clock

    def lease_window(self, i):
        l = self.leases[i]
        return l["start"], l["end"], self.clock >= l["end"], l.get("stopped", False)

    def renewed_by(self, i):
        return self.leases[i].get("renewed_by", 0)

    def sign_claim(self, msg):
        self.claims.append(msg)
        return "sig"

    def settle_success(self, i):
        pass

    def release_failed(self, i):
        pass


class FakeRpc:
    calls = []

    def __init__(self, endpoint):
        pass

    def map_exit(self, seller, full_tunnel=True):
        FakeRpc.calls.append(("map", seller))
        return {"result": "ok"}

    def unmap_exit(self, seller):
        FakeRpc.calls.append(("unmap", seller))
        return {"result": "ok"}

    def list_exits(self):
        return {"result": [{"address": SELLER, "exit_capable": True, "session_live": True}]}


def _home(lease_id=7, pending=True, dns="10.195.0.53"):
    home = Path(tempfile.mkdtemp())
    (home / "visibility.ack").write_text("I UNDERSTAND\n")
    st = state.State(home)
    if pending:
        st.data["pending_buy"] = {"seller": SELLER, "hold_id": lease_id, "contract": CONTRACT,
                                  "chainid": 42161, "vendor_id": "0x" + "bb" * 32, "amount": 500_000,
                                  "dns": dns}
    st.data["lease"] = {"seller": SELLER, "contract": CONTRACT, "chainid": 42161, "current": lease_id,
                        "renewed_from": [], "auto_renew": True, "client_sesh": ME,
                        "spool": str(home / "spool"), "rate_per_hour": 500_000, "dns": dns}
    st.save()
    return home


class _Base(unittest.TestCase):
    def setUp(self):
        FakeRpc.calls = []

    STOCK = {"tun": "sr-tun0", "dns": ["127.3.2.1"], "domains": ["~sesh", "~snode"], "default_route": False}

    def _run(self, home, ch, *extra):
        self.dns_calls = getattr(self, "dns_calls", [])
        cur = getattr(self, "dns_cur", None)
        if cur is None:
            self.dns_cur = cur = dict(self.STOCK)
        def apply(saved, listen=cli.tunneldns.LISTEN):
            self.dns_calls.append(("apply", saved))
            self.dns_cur.update(dns=[listen], domains=["~.", "~sesh", "~snode"], default_route=True)
        def restore(saved):
            self.dns_calls.append(("restore", saved))
            self.dns_cur.clear(); self.dns_cur.update(saved)
        with mock.patch.object(cli.tunneldns, "resolved_active", lambda: getattr(self, "resolved", True)), \
             mock.patch.object(cli.tunneldns, "snapshot", lambda tun: dict(self.dns_cur)), \
             mock.patch.object(cli.tunneldns, "start_proxy", getattr(self, "start_proxy", lambda *a, **k: 4242)), \
             mock.patch.object(cli.tunneldns, "stop_proxy", lambda home: self.dns_calls.append(("stop",)) or True), \
             mock.patch.object(cli.tunneldns, "proxy_pid", lambda home: 4242), \
             mock.patch.object(cli.tunneldns, "apply", apply), \
             mock.patch.object(cli.tunneldns, "restore", restore), \
             mock.patch.object(cli.tunneldns, "tun_networks", lambda tun: getattr(self, "tun_nets", ["172.16.0.0/16"])), \
             mock.patch.object(cli, "load_listings", getattr(self, "board", lambda *a, **k: [])), \
             mock.patch.object(cli.chain, "ChainCfg", lambda **kw: ch), \
             mock.patch.object(cli.keys, "resolve", lambda *a, **k: ("0x" + "11" * 32, "test")), \
             mock.patch.object(cli.rpc, "Rpc", FakeRpc), \
             mock.patch.object(cli, "_guess_wan", lambda: "eth0"), \
             mock.patch.object(cli, "_own_sesh", lambda: ME):
            return cli.main(["--home", str(home), *extra])

    def _st(self, home):
        return json.loads((home / "state.json").read_text())



class ConnectFreshUser(_Base):
    def test_connect_needs_no_client_sesh_flag(self):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        self.assertIn(ME, ch.claims[0])
        claim = json.loads((home / "spool" / "hold-7.json").read_text())
        self.assertEqual(claim["client_sesh"], ME)

    def test_reconnect_after_disconnect_inside_paid_hour(self):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        self.assertIsNone(self._st(home)["pending_buy"])
        self.assertEqual(self._run(home, ch, "disconnect"), 0)
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        s = self._st(home)
        self.assertEqual(s["connected"]["hold_id"], "7")
        self.assertTrue(s["connected"]["resumed"])
        self.assertEqual(FakeRpc.calls.count(("map", SELLER)), 2)

    def test_reconnect_follows_renewal(self):
        home = _home(pending=False)
        ch = FakeChain({7: {"start": 6_400, "end": 10_000, "renewed_by": 8}, 8: {"start": 10_000, "end": 13_600}},
                       now=10_500)
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(self._st(home)["lease"]["current"], 8)
        self.assertTrue((home / "spool" / "hold-8.json").exists())

    def test_reconnect_refused_after_lease_ended(self):
        home = _home(pending=False)
        ch = FakeChain({7: {"start": 6_000, "end": 9_600}}, now=10_000)
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("ended", err.getvalue())
        self.assertIn("srnet buy", err.getvalue())
        self.assertEqual(FakeRpc.calls, [])

    def test_stale_pending_buy_for_ended_lease_is_refused(self):
        home = _home()  # pending_buy for lease 7 still recorded, but lease 7 ran out
        ch = FakeChain({7: {"start": 6_000, "end": 9_600}}, now=10_000)
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)
        self.assertEqual(FakeRpc.calls, [])

    def test_reconnect_refused_when_seller_stopped(self):
        home = _home(pending=False)
        ch = FakeChain({7: {"start": 9_000, "end": 12_600, "stopped": True}})
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)
        self.assertEqual(FakeRpc.calls, [])

    def test_other_seller_needs_a_buy(self):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        other = "a" * 52 + ".sesh"
        self.assertEqual(self._run(home, ch, "connect", other, "--no-killswitch"), 1)

    def test_missing_nft_refuses_connect_clearly(self):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        err = _io.StringIO()
        with mock.patch.object(killswitch, "find_nft", lambda: None), mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, ch, "connect", SELLER), 1)
        self.assertIn("sudo apt install nftables", err.getvalue())
        self.assertIn("--no-killswitch", err.getvalue())
        self.assertEqual(FakeRpc.calls, [], "nothing is mapped when the kill switch cannot be armed")
        self.assertIsNotNone(self._st(home)["pending_buy"], "a fresh buy stays connectable")


class ConnectChainUnreachable(_Base):
    """The kill switch left armed after the tunnel dropped blocks the chain RPC."""
    class Offline(FakeChain):
        def chainid(self):
            raise cli.chain.ChainError("can't reach RPC https://arb1.arbitrum.io/rpc: no DNS")

    def test_reconnect_uses_recorded_lease_end(self):
        import time as _t
        home = _home(pending=False)
        s = json.loads((home / "state.json").read_text())
        s["lease"]["end"] = int(_t.time()) + 1800
        (home / "state.json").write_text(json.dumps(s))
        self.assertEqual(self._run(home, self.Offline({}), "connect", SELLER, "--no-killswitch"), 0)
        self.assertTrue(self._st(home)["connected"]["resumed"])

    def test_reconnect_refused_without_a_recorded_end(self):
        home = _home(pending=False)
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, self.Offline({}), "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("srnet disconnect", err.getvalue())
        self.assertEqual(FakeRpc.calls, [])

    def test_connect_records_lease_end(self):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(self._st(home)["lease"]["end"], 12_600)


class TunnelDNS(_Base):
    def _connect(self, *extra):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        return home, ch, self._run(home, ch, "connect", SELLER, "--no-killswitch", *extra)

    def test_connect_routes_dns_through_exit_and_disconnect_restores_exactly(self):
        home, ch, rc = self._connect()
        self.assertEqual(rc, 0)
        s = self._st(home)
        self.assertEqual(s["tunnel_dns"]["saved"], self.STOCK)
        self.assertEqual(s["connected"]["dns"], "tunnel")
        self.assertEqual(self.dns_cur["domains"][0], "~.")
        self.assertTrue(self.dns_cur["default_route"])
        self.assertEqual(self._run(home, ch, "disconnect"), 0)
        self.assertEqual(self.dns_cur, self.STOCK)
        self.assertIsNone(self._st(home)["tunnel_dns"])
        kinds = [c[0] for c in self.dns_calls]
        self.assertEqual(kinds, ["apply", "restore", "stop"])

    def test_disconnect_restores_dns_before_unmapping(self):
        home, ch, _ = self._connect()
        order = []
        self.dns_calls = order
        FakeRpc.calls = order
        self._run(home, ch, "disconnect")
        self.assertLess(order.index(("restore", self.STOCK)), order.index(("unmap", SELLER)))

    def test_dns_failure_unmaps_instead_of_leaking(self):
        def boom(*a, **k):
            raise cli.tunneldns.TunnelDNSError("DNS over the exit does not answer")
        self.start_proxy = boom
        home, ch, rc = self._connect()
        self.assertEqual(rc, 1)
        self.assertIn(("unmap", SELLER), FakeRpc.calls)
        self.assertIsNone(self._st(home).get("connected"))
        self.assertEqual(self.dns_cur, self.STOCK)

    def test_refuses_without_resolved(self):
        self.resolved = False
        home, ch, rc = self._connect()
        self.assertEqual(rc, 1)
        self.assertEqual(FakeRpc.calls, [])

    def test_system_dns_flag_leaves_dns_alone(self):
        self.resolved = False
        home, ch, rc = self._connect("--system-dns")
        self.assertEqual(rc, 0)
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(self._st(home)["connected"]["dns"], "system")

    def test_second_connect_keeps_the_original_snapshot(self):
        home, ch, _ = self._connect()
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)  # no disconnect between
        self.assertEqual(self._st(home)["tunnel_dns"]["saved"], self.STOCK)

    def test_our_settings_without_a_record_fall_back_to_stock(self):
        self.dns_cur = {"tun": "sr-tun0", "dns": [cli.tunneldns.LISTEN], "domains": ["~.", "~sesh", "~snode"],
                        "default_route": True}
        home, ch, _ = self._connect()
        self.assertEqual(self._st(home)["tunnel_dns"]["saved"], self.STOCK)

    def test_status_words(self):
        home, ch, _ = self._connect()
        out = _io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(cli.killswitch.KillSwitch, "status", lambda self: None), \
             mock.patch.object(cli.killswitch, "find_nft", lambda: "/usr/sbin/nft"):
            self._run(home, ch, "status")
        j = json.loads(out.getvalue())
        self.assertEqual(j["killswitch"], "unknown (needs sudo to check)")
        self.assertIn("through the exit", j["dns"])


class DisconnectWhenNotConnected(_Base):
    def test_leftover_check_needing_sudo_is_a_note_not_a_failure(self):
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        def needs_sudo(self):
            raise cli.killswitch.KillSwitchError("needs root and sudo did not allow it")
        err = _io.StringIO()
        with mock.patch.object(cli.killswitch.KillSwitch, "disarm", needs_sudo), mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, ch, "disconnect"), 0)
        self.assertIn("could not check", err.getvalue())


class ExitResolver(_Base):
    def test_forwarder_gets_the_exit_resolver_from_the_buy(self):
        seen = []
        self.start_proxy = lambda home, tun, upstream, *a, **k: seen.append(upstream) or 4242
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(seen, ["10.195.0.53"])
        self.assertEqual(self._st(home)["connected"]["exit_dns"], "10.195.0.53")

    def test_refuses_an_exit_without_resolver_before_mapping(self):
        home, ch = _home(dns=None), FakeChain({7: {"start": 9_000, "end": 12_600}})
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("no DNS resolver", err.getvalue())
        self.assertFalse([c for c in FakeRpc.calls if c[0] == "map"])
        self.assertFalse([c for c in self.dns_calls if c[0] == "apply"])

    def test_resolver_learned_from_the_board_for_an_old_lease(self):
        doc = {"listing": {"seller": SELLER, "dns": "10.195.0.53"}}
        self.board = lambda *a, **k: [doc]
        home, ch = _home(dns=None), FakeChain({7: {"start": 9_000, "end": 12_600}})
        self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(self._st(home)["connected"]["exit_dns"], "10.195.0.53")

    def test_board_unreachable_and_no_record_refuses(self):
        def down(*a, **k):
            raise OSError("no route")
        self.board = down
        home, ch = _home(dns=None), FakeChain({7: {"start": 9_000, "end": 12_600}})
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("board is unavailable", err.getvalue())

    def test_public_resolver_in_listing_is_ignored(self):
        self.board = lambda *a, **k: [{"listing": {"seller": SELLER, "dns": "1.1.1.1"}}]
        home, ch = _home(dns=None), FakeChain({7: {"start": 9_000, "end": 12_600}})
        with mock.patch("sys.stderr", _io.StringIO()):
            self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)

    def test_resolver_inside_own_tunnel_range_refuses(self):
        self.tun_nets = ["10.195.0.0/16"]
        home, ch = _home(), FakeChain({7: {"start": 9_000, "end": 12_600}})
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("inside your own tunnel range", err.getvalue())

    def test_system_dns_connects_without_exit_resolver(self):
        home, ch = _home(dns=None), FakeChain({7: {"start": 9_000, "end": 12_600}})
        with mock.patch("sys.stderr", _io.StringIO()):
            self.assertEqual(self._run(home, ch, "connect", SELLER, "--no-killswitch", "--system-dns"), 0)


class TunnelDNSHelpers(unittest.TestCase):
    def test_snapshot_parses_resolvectl(self):
        outs = {"dns": "Link 12 (sr-tun0): 127.3.2.1 [::1]:53\n", "domain": "Link 12 (sr-tun0): ~sesh ~snode\n",
                "default-route": "Link 12 (sr-tun0): no\n"}
        def run(cmd, capture_output, text):
            class R: returncode = 0; stdout = outs[cmd[1]]; stderr = ""
            return R()
        with mock.patch.object(cli.tunneldns, "resolvectl", lambda: "/usr/bin/resolvectl"), \
             mock.patch.object(cli.tunneldns.subprocess, "run", run):
            snap = cli.tunneldns.snapshot("sr-tun0")
        self.assertEqual(snap, {"tun": "sr-tun0", "dns": ["127.3.2.1", "[::1]:53"], "domains": ["~sesh", "~snode"],
                                "default_route": False})

    def test_apply_and_restore_scripts(self):
        seen = []
        with mock.patch.object(cli.tunneldns, "resolvectl", lambda: "/usr/bin/resolvectl"), \
             mock.patch.object(cli.tunneldns, "_root_sh", seen.append):
            saved = {"tun": "sr-tun0", "dns": ["127.3.2.1"], "domains": ["~sesh", "~snode"], "default_route": False}
            cli.tunneldns.apply(saved)
            cli.tunneldns.restore(saved)
            cli.tunneldns.restore(dict(saved, dns=[], domains=[]))
        self.assertIn("dns sr-tun0 127.3.2.2:5053", seen[0])
        self.assertIn("domain sr-tun0 '~.' '~sesh' '~snode'", seen[0])
        self.assertIn("default-route sr-tun0 true", seen[0])
        self.assertIn("dns sr-tun0 127.3.2.1 ", seen[1])
        self.assertIn("default-route sr-tun0 false", seen[1])
        self.assertIn("dns sr-tun0 '' ", seen[2])


class KillSwitchDefaults(unittest.TestCase):
    def test_default_tun_is_sr_tun0(self):
        self.assertEqual(killswitch.KillSwitch().tun, "sr-tun0")
        scripts = []
        ks = killswitch.KillSwitch()
        ks._nft_f = lambda script, check=True: scripts.append(script)
        ks.disarm = lambda: False
        ks.arm("eth0")
        self.assertIn('oifname "sr-tun0" accept', scripts[0])

    def test_missing_nft(self):
        with mock.patch.object(killswitch, "find_nft", lambda: None):
            ks = killswitch.KillSwitch()
            with self.assertRaises(killswitch.KillSwitchError) as e:
                ks.arm("eth0")
            self.assertIn("apt install nftables", str(e.exception))
            self.assertIsNone(ks.status())
            self.assertFalse(ks.disarm())

    def test_non_root_uses_sudo(self):
        ks = killswitch.KillSwitch()
        with mock.patch.object(killswitch, "find_nft", lambda: "/usr/sbin/nft"), \
             mock.patch.object(killswitch.os, "geteuid", lambda: 1000), \
             mock.patch.object(killswitch.shutil, "which", lambda n: "/usr/bin/sudo"):
            self.assertEqual(ks._argv(["list", "tables"], interactive=False)[:3], ["sudo", "-n", "/usr/sbin/nft"])


class SeshDnsDefault(unittest.TestCase):
    def test_default_is_session_router_stock_listener(self):
        import importlib, os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SRNET_SESH_DNS", None)
            import seshdns
            importlib.reload(seshdns)
            self.assertEqual(seshdns.DEFAULT_DNS, "127.3.2.1:53")


if __name__ == "__main__":
    unittest.main()
