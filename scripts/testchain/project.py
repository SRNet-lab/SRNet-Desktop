"""Throwaway Foundry project for local anvil tests: contracts/SRNetBalance.sol plus the test mocks.

Built once per process in a temp dir (forge needs a project layout). Test chains only.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SOURCE = ROOT / "contracts" / "SRNetBalance.sol"
_dir: Path | None = None


def project_dir() -> Path:
    """src/SRNetBalance.sol and test/Mock*.sol, same compiler settings as the deployed contract."""
    global _dir
    if _dir is None:
        d = Path(tempfile.mkdtemp(prefix="srnet-testchain-"))
        atexit.register(shutil.rmtree, d, True)
        (d / "src").mkdir()
        (d / "test").mkdir()
        shutil.copy(SOURCE, d / "src" / "SRNetBalance.sol")
        for f in HERE.glob("*.sol"):
            shutil.copy(f, d / "test" / f.name)
        (d / "foundry.toml").write_text('[profile.default]\nsrc = "src"\nout = "out"\nlibs = []\n'
                                        'solc_version = "0.8.26"\noptimizer = true\noptimizer_runs = 200\n')
        _dir = d
    return _dir
