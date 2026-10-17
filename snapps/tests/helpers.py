import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mbfmt as F
from mbhost import host as H
from mbhost.node import serve, url_of


def start_host(tmp, board="demo", pow_bits=8, rate=None, mirrors=None, clock=time.time, **pol):
    """Test host: by default new keys need the same (low) PoW and have generous limits."""
    root, hk = F.new_key(), F.new_key()
    d = F.make_delegation(root, board, F.pub_hex(hk), int(time.time()) + 30 * 86400)
    p = {"pow_bits": pow_bits, "new_key_pow_bits": pow_bits, "new_key_rate": [[1000, 3600]]}
    if rate is not None:
        p["rate"] = rate
    p.update(pol)
    h = H.Host(tmp, board, hk, d, p, mirrors=mirrors, clock=clock)
    srv = serve(h)
    return h, srv, url_of(srv), F.pub_hex(root)


def tmpdir():
    return tempfile.mkdtemp(prefix="mbtest-")
