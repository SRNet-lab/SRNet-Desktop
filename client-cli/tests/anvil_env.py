"""Local anvil + forge harness for chain tests. Skipped when foundry is not installed. Test funds only."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "contracts" / "SRNetBalance.sol"
FOUNDRY = Path.home() / ".foundry" / "bin"
# anvil's well-known deterministic dev keys (test-only)
KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
]


def _bin(name: str) -> str | None:
    return shutil.which(name) or (str(FOUNDRY / name) if (FOUNDRY / name).exists() else None)


def available() -> bool:
    return bool(_bin("anvil") and _bin("forge") and SOURCE.is_file())


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _create(rpc: str, target: str, args: list[str]) -> str:
    cmd = [_bin("forge"), "create", "--broadcast", "--rpc-url", rpc, "--private-key", KEYS[0], target]
    if args:
        cmd += ["--constructor-args", *args]
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from testchain.project import project_dir
    out = subprocess.run(cmd, cwd=project_dir(), capture_output=True, text=True, timeout=600)
    m = re.search(r"Deployed to: (0x[0-9a-fA-F]{40})", out.stdout)
    if not m:
        raise RuntimeError(f"forge create {target} failed: {out.stdout[-800:]} {out.stderr[-800:]}")
    return m.group(1)


def start() -> dict:
    port = _port()
    proc = subprocess.Popen([_bin("anvil"), "--port", str(port), "--silent"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    rpc = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        usdc = _create(rpc, "test/MockUSDCPermit.sol:MockUSDCPermit", [])
        owner = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        vault = _create(rpc, "test/MockCfVault.sol:MockCfVault", [])
        bal = _create(rpc, "src/SRNetBalance.sol:SRNetBalance",
                      [usdc, owner, owner, "20000000", "168", "300", vault, "1000000"])
    except Exception:
        proc.terminate()
        raise
    return {"proc": proc, "rpc": rpc, "contract": bal, "usdc": usdc, "keys": KEYS}


def stop(env: dict) -> None:
    env["proc"].terminate()
    try:
        env["proc"].wait(5)
    except subprocess.TimeoutExpired:
        env["proc"].kill()
