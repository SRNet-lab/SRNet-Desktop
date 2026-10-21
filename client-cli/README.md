# srnet

The SRNet client's `srnet` command: `srnet ui` (the app), `srnet doctor` and `srnet mail`. To
install, follow the main [README](../README.md); this page covers the command line in detail.

Session Router has to be installed and running first (steps 1 and 2 of the main README).
`srnet doctor` checks it.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

## Install without install.sh

Needs Python 3.10 or newer. From the top of this repository:

```sh
python3 -m venv ~/srnet-venv
~/srnet-venv/bin/pip install "./client-cli[qr]"
~/srnet-venv/bin/srnet doctor
```

Dependencies: `cryptography` and `pyzmq` (and `qrcode` for payment QR codes). Settings live in
`$SRNET_HOME` (default `~/.srnet`).

## Usage

```
srnet ui [--no-browser] [--config F]   the app (also plain `srnet`; `snapps` is an alias)
srnet doctor                           check Session Router, .sesh DNS, the demo host, clock, disk, firewall
srnet open LINK                        open an srnet:// link in the app (shows the join sheet)
srnet mail ...                         sealed mail (srnet mail -h)
```

## Add-ons

Extra commands and app tabs come from add-ons: Python packages with an entry point in the
`srnet.addons` group. The exits add-on is `srnet-exits`:

```sh
./install.sh --with-exits
```

or by hand, with the Python the client runs with:
`python -m pip install "srnet-exits @ git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack"`. It adds
`srnet exits`, `srnet buy`, `srnet paid`, `srnet connect`, `srnet status`, `srnet disconnect` and
`srnet verify`, its own `srnet doctor` checks, and an Exits tab. Without it those commands print
the install line and exit.

An add-on object has `name`, `title`, `version`, `register(subparsers)` (returns
`{command: fn(args, state)}`), `doctor(state, line)`, `start_ui_backend(state)` (returns
`http://127.0.0.1:<port>/#<token>`) and `ui_manifest()`. For a checkout without pip,
`SRNET_ADDON_MODULES="module:ATTR"` loads one by import; `SRNET_NO_ADDONS=1` turns them off.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
