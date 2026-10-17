"""SRNet exit market client."""
import sys as _sys
from pathlib import Path as _Path

__version__ = "0.1.0"

# vendor/ modules are imported by bare name. In a checkout they sit next to the package;
# after `pip install .` they ship inside it as srnet/vendor.
for _v in (_Path(__file__).resolve().parents[1] / "vendor", _Path(__file__).resolve().parent / "vendor"):
    if _v.is_dir() and str(_v) not in _sys.path:
        _sys.path.insert(0, str(_v))
