#!/usr/bin/env python3
"""Local end-to-end smoke. Everything runs on loopback with throwaway keys.

1. Typed SNApps demo (board, room, wiki, profile, shop, file drop, exit view, HTML viewer).
2. `srnet exits` against a locally served signed exit board, with a dead first copy (failover).
3. Phone-wallet top-up on a local anvil chain: plain USDC transfer to the user's own SRNet
   wallet, then `srnet topup` signs a permit and a relayer deposits it (no ETH in the wallet).

Needs anvil + forge (Foundry) for step 3; skipped with a note if missing.
"""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "client-cli"
PY = sys.executable
sys.path[:0] = [str(CLI), str(CLI / "vendor")]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def srnet(home, *args, env=None, timeout=300):
    e = {**os.environ, **(env or {})}
    return subprocess.run([PY, "-m", "srnet", "--home", str(home), *args], cwd=CLI, env=e,
                          capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


tmp = Path(tempfile.mkdtemp(prefix="srnet-smoke-"))

# 1. typed SNApps
p = subprocess.run([PY, "scripts/platform_demo.py", "--keys-dir", str(tmp / "demo-keys")],
                   cwd=ROOT / "snapps", capture_output=True, text=True, timeout=600)
last = (p.stdout.strip().splitlines() or ["?"])[-1]
check("typed SNApps demo", p.returncode == 0 and "passed" in last and last.split("/")[0] == last.split("/")[1].split()[0], last)

# 2. exits listing from a local signed board, first copy dead
import boardfmt as B
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from srnet import chain
L = B.L
bkey, skey = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
now = int(time.time())
listing = L.make(skey, payout="0x" + "11" * 20, vendor_id="0x" + "22" * 32, chainid=42161,
                 contract=chain.DEFAULT_CONTRACT, region="eu", price_per_hour_usdc="0.10", max_hours=24)
body = {"v": 1, "board": L.sesh_of(B.pub_raw(bkey)), "seq": 1, "issued": now, "expires": now + 3600,
        "max_listing_age_s": B.DEFAULT_MAX_LISTING_AGE_S, "listings": [listing]}
www = tmp / "board-www"; www.mkdir()
(www / "board.json").write_text(json.dumps(body))
(www / "board.sig").write_text(B.sign_board(bkey, body))
class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass
srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(www)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
home = tmp / "home"; home.mkdir()
dead = f"http://127.0.0.1:{free_port()}/"
(home / "config.json").write_text(json.dumps({"board_urls": [dead, f"http://127.0.0.1:{srv.server_address[1]}/"],
                                               "board_pin": body["board"]}))
r = srnet(home, "exits")
check("exits listing (failover past dead copy)", r.returncode == 0 and listing["listing"]["seller"] in r.stdout,
      (r.stderr.strip().splitlines() or [""])[-1])
(www / "board.json").write_text(json.dumps({**body, "seq": 2}))  # tampered: signature no longer matches
r = srnet(home, "exits")
check("tampered board rejected, cache used", r.returncode == 0 and "cached copy" in r.stderr,
      (r.stderr.strip().splitlines() or [""])[-1])

# 3. phone-wallet top-up on anvil
anvil, forge = shutil.which("anvil"), shutil.which("forge")
if not (anvil and forge):
    print("[SKIP] top-up: anvil/forge not installed")
else:
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    M = "test test test test test test test test test test test junk"  # public anvil dev mnemonic
    dev = [Account.from_mnemonic(M, account_path=f"m/44'/60'/0'/0/{i}") for i in range(3)]
    port = free_port(); rpc = f"http://127.0.0.1:{port}"
    av = subprocess.Popen([anvil, "--port", str(port), "--silent", "--base-fee", "10000000"], stdout=subprocess.DEVNULL)  # Arbitrum-like 0.01 gwei
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(urllib.request.Request(rpc, data=b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId"}',
                                                              headers={"Content-Type": "application/json"}), timeout=1); break
            except OSError:
                time.sleep(0.2)
        sys.path.insert(0, str(ROOT / "scripts"))
        from testchain.project import project_dir
        cdir = project_dir()
        def create(what, *args):
            out = subprocess.run([forge, "create", "--broadcast", "--rpc-url", rpc, "--private-key", "0x" + dev[0].key.hex().removeprefix("0x"),
                                  what, *(["--constructor-args", *args] if args else [])], cwd=cdir,
                                 capture_output=True, text=True, timeout=600, check=True).stdout
            return [l.split()[-1] for l in out.splitlines() if "Deployed to" in l][0]
        usdc = create("test/MockUSDCPermit.sol:MockUSDCPermit")
        vault = create("test/MockCfVault.sol:MockCfVault")
        owner = dev[0].address
        bal = create("src/SRNetBalance.sol:SRNetBalance", usdc, owner, owner, "20000000", "168", "300", vault, "1000000")
        ch0 = chain.ChainCfg(rpc=rpc, contract=bal, private_key="0x" + dev[0].key.hex().removeprefix("0x"))
        ch0.chainid()
        # user wallet: fresh keystore, zero ETH
        pw = "smoke-only-passphrase"
        user_home = tmp / "user"; user_home.mkdir()
        (user_home / "config.json").write_text(json.dumps({"rpc": rpc, "contract": bal}))
        r = srnet(user_home, "key", "new", env={"SRNET_KEYSTORE_PASSWORD": pw})
        from srnet import keys
        uaddr = keys.address_of(keys.default_path(user_home))
        # the "phone wallet" (dev[2]) sends plain USDC to the user's own address
        ch2 = chain.ChainCfg(rpc=rpc, contract=bal, private_key="0x" + dev[2].key.hex().removeprefix("0x"))
        ch2._send("mint(address,uint256)", dev[2].address, 50_000_000, to=usdc)
        ch2._send("transfer(address,uint256)", uaddr, 10_000_000, to=usdc)
        rport = free_port()
        rel = subprocess.Popen([PY, "-m", "srnet.relayer", "--contract", bal, "--rpc", rpc, "--port", str(rport),
                                "--max-gas-per-tx-wei", str(10**16)],  # anvil suggests a 1 gwei tip; Arbitrum does not
                               cwd=CLI, env={**os.environ, "SRNET_RELAYER_KEY": "0x" + dev[1].key.hex().removeprefix("0x")},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(1.5)
        r = srnet(user_home, "topup", "--amount", "10", "--relayer", f"http://127.0.0.1:{rport}",
                  env={"SRNET_KEYSTORE_PASSWORD": pw}, timeout=120)
        rel.terminate()
        out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        ok = False
        try:
            res = json.loads(out); avail = int(res["available"]); ok = avail == 9_650_000
        except Exception:
            avail = None
        check("QR top-up via relayer (anvil)", r.returncode == 0 and ok,
              f"10 USDC sent -> credit {avail} base units (expect 9650000 = 10 - 3% - 0.05); {r.stderr.strip()[-200:]}")
    finally:
        av.terminate()

srv.shutdown()
print(f"SMOKE {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
sys.exit(0 if all(results) else 1)
