#!/usr/bin/env python3
"""srnet: chat, mail, files and SNApps over Session Router.

  srnet ui                 the app on 127.0.0.1: SNApps, mail, files (also plain `srnet`)
  srnet doctor             check Session Router, .sesh DNS, the demo host, clock, disk, firewall
  srnet open LINK          open an srnet:// link in the app (shows the join sheet)
  srnet mail ...           sealed mail from the command line (srnet mail -h)
  srnet snapps             alias of `srnet ui` that doesn't open a browser

Exits (buying time on an SRNet exit and routing your traffic through it) are an add-on:
srnet-exits. Once it is installed, `srnet exits`, `srnet buy`, `srnet connect` and friends
appear here, and the app gets an Exits tab.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import addons, state


def _addon_missing(args, st: state.State) -> int:
    print(addons.EXITS_HINT, file=sys.stderr)
    return 2


def cmd_doctor(args, st: state.State) -> int:
    """Setup preflight (router, DNS, demo, clock...), then checks from installed add-ons.
    Exit 0 if nothing failed."""
    from . import preflight
    rep = preflight.run(skip=tuple(args.skip or ()))
    print(preflight.text(rep), end="")
    try:
        path = preflight.write_report(rep, args.report, st.root)
        print(f"report: {path}  (send this file if you need help)", flush=True)
    except (OSError, RuntimeError) as e:
        print(f"report not written: {e}", flush=True)
    if args.setup_only:
        return 1 if rep["preflight"] == "FAIL" else 0
    bad = 0

    def line(ok, what, detail=""):
        nonlocal bad
        tag = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        bad += ok is False
        print(f"[{tag}] {what}{': ' + detail if detail else ''}")

    line(sys.version_info >= (3, 10), "python", sys.version.split()[0])
    for mod, pkg in (("cryptography", "cryptography"), ("zmq", "pyzmq")):
        try:
            __import__(mod)
            line(True, pkg)
        except ImportError:
            line(False, pkg, "not installed; run: pip install .")
    found = addons.load()
    if not found:
        line(None, "add-ons", "none. " + addons.EXITS_HINT)
    for name, a in sorted(found.items()):
        try:
            a.doctor(st, line)
        except Exception as e:
            line(False, f"add-on {name}", str(e)[:200])
    pf = rep.get("preflight", "OK")
    if bad:
        print(f"{bad} problem(s) above")
    elif pf == "FAIL":
        print("setup preflight failed; see CHECK/FIX lines above")
    elif pf == "WARN":
        print("setup preflight has warnings above (often Session Router)")
    else:
        print("all good")
    return 1 if bad or pf == "FAIL" else 0


def cmd_snapps(args, st: state.State) -> int:
    """Start the typed-SNApp client (signed records, sandboxed templates, no-script HTML viewer)."""
    root = Path(os.environ.get("SRNET_SNAPPS_DIR") or Path(__file__).resolve().parents[2] / "snapps")
    if not (root / "typed" / "core.py").exists():
        print(f"typed SNApps not found at {root}; set SRNET_SNAPPS_DIR", file=sys.stderr)
        return 2
    data = args.data or str(st.root / "snapps")
    # shipped defaults (lobby + directory pre-joined, default mail host), then the user's config on top
    user_cfg = Path(args.config) if args.config else st.root / "client.json"
    cmd = [sys.executable, "-m", "typed.core", "--data", data, "--port", str(args.port)]
    if args.config or user_cfg.exists():
        if not user_cfg.exists():
            print(f"config not found: {user_cfg}", file=sys.stderr)
            return 2
        cmd += ["--config", str(user_cfg.resolve())]
    return _run_child(cmd, str(root))


def _run_child(cmd, cwd) -> int:
    """Run the core; SIGTERM / Ctrl-C to `srnet ui` must not orphan it (it would keep the port)."""
    child = subprocess.Popen(cmd, cwd=cwd)
    if os.name != "nt":
        import signal
        signal.signal(signal.SIGTERM, lambda *a: (child.terminate(), sys.exit(143)))
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        raise
    finally:
        if child.poll() is None:
            child.terminate()


def cmd_ui(args, st: state.State) -> int:
    """The SRNet app: SNApps shell (opens on Home), plus a tab for each installed add-on (Exits).
    Add-on backends run on token-protected loopback ports inside this process; the core bridges
    the shell to them. `srnet snapps` is an alias. If a client already answers on --port, just open it."""
    import socket
    import threading
    import time
    import webbrowser
    home = f"http://127.0.0.1:{args.port}/"

    def up(port=None) -> bool:
        try:
            socket.create_connection(("127.0.0.1", port or args.port), timeout=1).close()
            return True
        except OSError:
            return False
    # one app per home: a second core on the same data dir would block on its database
    pf = st.root / "ui.port"
    try:
        prev = int(pf.read_text().strip())
    except (OSError, ValueError):
        prev = None
    if prev and prev != args.port and up(prev):
        args.port, home = prev, f"http://127.0.0.1:{prev}/"
    if up():
        print(f"srnet ui: {home} (already running)", flush=True)
        if not args.no_browser:
            webbrowser.open(home)
        return 0
    os.environ["SRNET_ADDONS"] = addons.ui_env(addons.load(), st)
    if not args.no_browser:
        def _open():
            for _ in range(240):
                if up():
                    webbrowser.open(home)
                    return
                time.sleep(0.5)
        threading.Thread(target=_open, daemon=True).start()
    print(f"srnet ui: {home}\n(local only; Ctrl-C to stop)", flush=True)
    st.root.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(args.port))
    return cmd_snapps(args, st)


def cmd_open(args, st: state.State) -> int:
    """srnet:// link handler: validate the link, then open the running client's join sheet.
    Nothing is joined until you click Join there."""
    root = Path(os.environ.get("SRNET_SNAPPS_DIR") or Path(__file__).resolve().parents[2] / "snapps")
    sys.path.insert(0, str(root))
    from typed import joins
    try:
        joins.parse(args.link)
    except joins.LinkError as e:
        print(f"not a valid SRNet link: {e}", file=sys.stderr)
        return 2
    from urllib.parse import quote
    url = f"http://127.0.0.1:{args.port}/?join=" + quote(args.link.strip(), safe="")
    print(url)
    if not args.print_only:
        import webbrowser
        webbrowser.open(url)
    return 0


def cmd_mail(args, st: state.State) -> int:
    """Sealed SRNet mail (typed.mailcli), using the same identity as `srnet snapps`."""
    root = Path(os.environ.get("SRNET_SNAPPS_DIR") or Path(__file__).resolve().parents[2] / "snapps")
    if not (root / "typed" / "mailcli.py").exists():
        print(f"typed SNApps not found at {root}; set SRNET_SNAPPS_DIR", file=sys.stderr)
        return 2
    cmd = [sys.executable, "-m", "typed.mailcli", "--data", str(st.root / "snapps"), *args.rest]
    return subprocess.call(cmd, cwd=str(root))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="srnet", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="state dir (default ~/.srnet or $SRNET_HOME)")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")
    ap.set_defaults(port=48920, no_browser=False, config=None, data=None)

    dr = sub.add_parser("doctor", help="check my setup (router, DNS, demo...) and installed add-ons")
    dr.add_argument("--setup-only", action="store_true", help="only the setup preflight")
    dr.add_argument("--report", help="report file (default %%LOCALAPPDATA%%\\SRNet\\preflight-report.txt)")
    dr.add_argument("--skip", action="append", help="skip a check by name (repeatable), e.g. --skip demo")

    u = sub.add_parser("ui", help="the SRNet app on 127.0.0.1 (opens your browser); the default")
    sn = sub.add_parser("snapps", help="alias of ui")
    for x in (u, sn):
        x.add_argument("--no-browser", action="store_true", help="print the URL only")
        x.add_argument("--config", help="your client.json, layered on the shipped defaults "
                       "(default <home>/client.json if it exists)")
        x.add_argument("--data", help="data dir (default <home>/snapps)")
        x.add_argument("--port", type=int, default=48920)
    sn.set_defaults(no_browser=True)  # old behaviour: `srnet snapps` didn't open a browser

    op = sub.add_parser("open", help="open an srnet:// link in the SNApps client (shows the join sheet)")
    op.add_argument("link")
    op.add_argument("--port", type=int, default=48920)
    op.add_argument("--print-only", action="store_true", help="print the URL, don't open a browser")
    ml = sub.add_parser("mail", help="sealed mail: hosts, address, send, fetch, list, read (see: srnet mail -h)",
                        add_help=False)
    ml.add_argument("rest", nargs=argparse.REMAINDER)

    cmds = {"doctor": cmd_doctor, "ui": cmd_ui, "snapps": cmd_ui, None: cmd_ui, "open": cmd_open, "mail": cmd_mail}
    for name, a in sorted(addons.load().items()):
        try:
            for c, fn in a.register(sub).items():
                cmds.setdefault(c, fn)
        except Exception as e:
            print(f"note: add-on {name} commands not available: {e}", file=sys.stderr)
    for c in addons.EXITS_COMMANDS:
        if c not in cmds:  # one-line install hint instead of "invalid choice"
            sub.add_parser(c, help="exits add-on (not installed)", add_help=False).add_argument(
                "rest", nargs=argparse.REMAINDER)
            cmds[c] = _addon_missing

    args, extra = ap.parse_known_args(argv)
    if args.cmd == "mail":
        args.rest = extra + args.rest
    elif extra and cmds.get(args.cmd) is not _addon_missing:
        ap.error("unrecognized arguments: " + " ".join(extra))
    st = state.State(Path(args.home) if args.home else None)
    try:
        return cmds[args.cmd](args, st)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
