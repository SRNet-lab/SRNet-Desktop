#!/usr/bin/env python3
"""Local test host for trying the client on one PC (Windows, macOS or Linux). Loopback only.

Starts, on 127.0.0.1:
  - a hub with a chat room `lobby` and an encrypted file drop `files`  (port 48940)
  - a mail host                                                        (port 48941)
Keys and data persist in --data (default ./srnet-test-host). Writes <data>/client.json for
`srnet snapps --config`, with the mail host preset and no collections (you join by link),
and prints the srnet:// invite links. Ctrl+C stops it. Touches nothing outside loopback.

  python scripts/local_test_host.py [--data DIR] [--port 48940] [--mail-port 48941]
"""
import argparse, json, os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mbfmt as F  # noqa: E402
from typed import joins as J, mailhost as MH  # noqa: E402
from typed.host import load_or_make_keys  # noqa: E402
from typed.hub import Hub  # noqa: E402
from typed.node import serve  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="srnet-test-host")
    ap.add_argument("--port", type=int, default=48940)
    ap.add_argument("--mail-port", type=int, default=48941)
    a = ap.parse_args(argv)
    d = os.path.abspath(a.data)
    root, hk, _ = load_or_make_keys(os.path.join(d, "hub"), "hub")
    hub = Hub(os.path.join(d, "hub"), root, hk, [("room", "lobby"), ("drop", "files")], {})
    serve(hub, "127.0.0.1", a.port)
    MH.serve(MH.Host(os.path.join(d, "mail")), "127.0.0.1", a.mail_port)
    pin, host = F.pub_hex(root), f"127.0.0.1:{a.port}"
    links = {n: J.make(host, k, n, pin) for k, n in (("room", "lobby"), ("drop", "files"))}
    mail = f"http://127.0.0.1:{a.mail_port}"
    cfgp = os.path.join(d, "client.json")
    with open(cfgp, "w") as f:
        json.dump({"collections": [{"id": "lobby", "disabled": True}, {"id": "directory", "disabled": True}],  # loopback only: hide the shipped demo lobby/directory
                   "mail": {"hosts": [mail], "attach_drop": "files"}}, f, indent=2)
    info = {"room_link": links["lobby"], "files_link": links["files"], "mail_host": mail,
            "client_config": cfgp, "root_pin": pin}
    with open(os.path.join(d, "links.json"), "w") as f:
        json.dump(info, f, indent=2)
    print("SRNet local test host (loopback only). Ctrl+C to stop.")
    for k, v in info.items():
        print(f"  {k:14s}{v}")
    print("READY", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
