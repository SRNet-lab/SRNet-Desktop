"""Local anvil chain with MockUSDCPermit + SRNetBalance deployed (tests and smoke only).

Uses the public anvil dev mnemonic; nothing here touches a real network.
"""
import os, shutil, socket, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MNEMONIC = "test test test test test test test test test test test junk"  # public anvil dev mnemonic


def available() -> bool:
    return bool(shutil.which("anvil") and shutil.which("forge")) and (ROOT / "contracts" / "SRNetBalance.sol").is_file()


def _port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def dev_keys(n=5):
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    return ["0x" + Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{i}").key.hex().removeprefix("0x")
            for i in range(n)]


def start():
    port = _port(); rpc = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([shutil.which("anvil"), "--port", str(port), "--silent", "--base-fee", "10000000"],
                            stdout=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(urllib.request.Request(
                rpc, data=b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId"}',
                headers={"Content-Type": "application/json"}), timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    keys = dev_keys()
    sys.path.insert(0, str(ROOT / "scripts"))
    from testchain.project import project_dir
    cdir = project_dir()

    def create(what, *args):
        out = subprocess.run([shutil.which("forge"), "create", "--broadcast", "--rpc-url", rpc, "--private-key", keys[0],
                              what, *(["--constructor-args", *args] if args else [])], cwd=cdir,
                             capture_output=True, text=True, timeout=600, check=True).stdout
        return [l.split()[-1] for l in out.splitlines() if "Deployed to" in l][0]
    from eth_account import Account
    owner = Account.from_key(keys[0]).address
    usdc = create("test/MockUSDCPermit.sol:MockUSDCPermit")
    vault = create("test/MockCfVault.sol:MockCfVault")
    bal = create("src/SRNetBalance.sol:SRNetBalance", usdc, owner, owner, "20000000", "168", "300", vault, "1000000")
    return {"rpc": rpc, "usdc": usdc, "contract": bal, "keys": keys, "proc": proc}


def stop(env):
    env["proc"].terminate()
