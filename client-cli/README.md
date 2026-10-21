# srnet

The SRNet client: `srnet ui` (the app), `srnet doctor` and `srnet mail` from the command line.
For the normal install, use `./install.sh` at the top of this repository (see the main
[README](../README.md)); this page covers the command line in detail.

Session Router is a separate prerequisite: install and run it yourself. Its API must answer on
`127.0.0.1:1190`; `srnet doctor` checks it.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

## Install without install.sh

Needs Python 3.10+.

```sh
cd client-cli
pip install ".[qr]"
srnet doctor
```

Dependencies: `cryptography` and `pyzmq` (and `qrcode` for payment QR codes). Without installing, `python3 -m srnet` works from this directory once the
dependencies are present. Settings live in `$SRNET_HOME` (default `~/.srnet`).

## Usage

```
srnet ui [--no-browser] [--config F]   the app (also plain `srnet`; `snapps` is an alias)
srnet doctor                           check Session Router, .sesh DNS, the demo host, clock, disk, firewall
srnet open LINK                        open an srnet:// link in the app (shows the join sheet)
srnet mail ...                         sealed mail (srnet mail -h)
```

## Add-ons

Extra commands and app tabs come from add-ons: Python packages with an entry point in the
`srnet.addons` group. The SRNet exits add-on is `srnet-exits`:

```sh
python -m pip install "srnet-exits @ git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack"
```

(use the Python the client runs with; `./install.sh --with-exits` does this for you). It adds
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
