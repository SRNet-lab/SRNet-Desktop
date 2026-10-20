#!/usr/bin/env bash
# SRNet client installer for Linux and macOS. Installs into your user account only (no sudo).
#   ./install.sh               install or upgrade
#   ./install.sh --with-exits  also install the exits add-on (buy time on an exit, connect through it)
#   ./install.sh --uninstall
set -euo pipefail
PREFIX="${SRNET_PREFIX:-$HOME/.local/share/srnet-client}"
BIN="${SRNET_BIN:-$HOME/.local/bin}"
SRC="$(cd "$(dirname "$0")" && pwd)"

UNINSTALL=0; WITH_EXITS=0
for a in "$@"; do
  case "$a" in
    --uninstall) UNINSTALL=1;;
    --with-exits) WITH_EXITS=1;;
    -h|--help) sed -n '2,5p' "$0"; exit 0;;
    *) echo "install.sh: unknown option $a (see --help)" >&2; exit 2;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  rm -rf "$PREFIX" "$BIN/srnet" "$BIN/srnet-relayer" "$HOME/.local/share/applications/srnet-ui.desktop" "$HOME/.local/share/applications/srnet-open.desktop"
  echo "Removed the SRNet client. Your settings in ~/.srnet were left in place."
  exit 0
fi
# The add-on comes from its git repo (or a local copy), never by bare name from PyPI.
EXITS_SRC="${SRNET_EXITS_SRC:-srnet-exits @ git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack}"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "Python 3.10 or newer is required." >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is required (found $("$PY" -V))." >&2; exit 1; }
"$PY" -c 'import venv, ensurepip' 2>/dev/null \
  || { echo "Python venv support is missing. On Debian/Ubuntu: sudo apt install python3-venv" >&2; exit 1; }

echo "Installing to $PREFIX"
mkdir -p "$PREFIX" "$BIN"
rm -rf "$PREFIX/app"
mkdir -p "$PREFIX/app"
cp -R "$SRC/client-cli" "$SRC/snapps" "$PREFIX/app/"
cp "$SRC/LICENSE" "$SRC/LICENSE-APACHE-2.0.txt" "$SRC/README.md" "$PREFIX/app/"
find "$PREFIX/app" -name __pycache__ -prune -exec rm -rf {} +

VPY="$PREFIX/venv/bin/python"
has_module() { [ -x "$VPY" ] && "$VPY" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('$1') else 1)"; }
HAD_EXITS=0; has_module srnet_exits && HAD_EXITS=1
if has_module eth_account || [ -e "$BIN/srnet-relayer" ]; then
  # An install from before the split: start a clean venv so no wallet libraries are left in it.
  echo "Removing the old client's wallet libraries"
  rm -rf "$PREFIX/venv"
fi
[ -x "$VPY" ] || "$PY" -m venv "$PREFIX/venv"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet "$PREFIX/app/client-cli[qr]"
if [ "$WITH_EXITS" = 1 ] || [ "$HAD_EXITS" = 1 ]; then
  echo "Installing the exits add-on"
  "$VPY" -m pip install --quiet --upgrade "$EXITS_SRC"
fi

cat > "$BIN/srnet" <<W
#!/usr/bin/env bash
export SRNET_SNAPPS_DIR="$PREFIX/app/snapps"
exec "$PREFIX/venv/bin/python" -m srnet "\$@"
W
chmod 755 "$BIN/srnet"
rm -f "$BIN/srnet-relayer"

if [ "$(uname -s)" = "Linux" ] && [ -d "$HOME/.local/share" ]; then
  mkdir -p "$HOME/.local/share/applications"
  sed "s#^Exec=.*#Exec=$BIN/srnet ui#" "$SRC/client-cli/srnet-ui.desktop" > "$HOME/.local/share/applications/srnet-ui.desktop"
  # srnet:// links open the client's join sheet (nothing joins until you click Join)
  sed "s#^Exec=.*#Exec=$BIN/srnet open %u#" "$SRC/client-cli/srnet-open.desktop" > "$HOME/.local/share/applications/srnet-open.desktop"
  mkdir -p "$HOME/.config"
  command -v xdg-mime >/dev/null && xdg-mime default srnet-open.desktop x-scheme-handler/srnet || true
  command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
fi

echo
echo "Done. Next steps:"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo
    echo "  $BIN is not on your PATH in this terminal. Run this, then try srnet again:"
    echo
    if [ "$BIN" = "$HOME/.local/bin" ]; then
      echo '    export PATH="$HOME/.local/bin:$PATH"'
    else
      echo "    export PATH=\"$BIN:\$PATH\""
    fi
    echo
    echo "  New terminals after your next login pick it up on Ubuntu automatically."
    ;;
esac
echo "  srnet doctor       check your setup (Session Router, DNS); writes a report"
echo "  srnet ui           open the app in your browser"
if command -v session-router >/dev/null 2>&1 || [ -x /usr/bin/session-router ] || \
   systemctl list-unit-files session-router-client.service >/dev/null 2>&1; then
  if systemctl is-active --quiet session-router-client 2>/dev/null; then
    echo "  Session Router: installed and running (session-router-client)"
  else
    echo "  Session Router: installed but not running; start it: sudo systemctl start session-router-client"
  fi
else
  echo "Session Router is a separate program (README, Install, step 1): install and start it before using .sesh names."
fi
[ "$WITH_EXITS" = 1 ] && echo "  srnet exits        list exits; buy time with: srnet buy --seller <id> --hours N"
[ "$WITH_EXITS" = 1 ] && [ "$(uname -s)" = "Darwin" ] && echo "  note: 'srnet connect' (routing traffic through an exit) is Linux-only for now."
exit 0
