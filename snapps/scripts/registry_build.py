"""Sign the bundled templates into typed/templates/registry.json (dev registry key).

The dev registry key lives OUTSIDE the repo (default $SRNET_REGISTRY_KEY or ~/.srnet-dev-keys/registry.key,
mode 600).  In production the registry root is offline and split; this script is
the stand-in.  Usage: python3 scripts/registry_build.py [--key PATH] [--ver 1.0.0]"""
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mbfmt as F
from typed.registry import read_dir, sign_entry

TYPES = {"board": ["board.post@1", "mod.hide@1"], "room": ["room.msg@1", "room.topic@1", "room.join@1"],
         "wiki": ["wiki.page@1", "wiki.rev@1", "wiki.revert@1", "wiki.protect@1"],
         "profile": ["id.profile@1", "id.rotate@1", "id.link@1"],
         "shop": ["shop.listing@1", "shop.withdraw@1", "shop.review@1", "shop.order@1"],
         "drop": ["drop.item@1", "drop.grant@1", "drop.revoke@1", "drop.request@1"],
         "exit": ["exit.listing@v2-legacy"], "card": ["snapp.card@legacy"],
         "directory": ["directory.entry@1", "id.profile@1"]}

ap = argparse.ArgumentParser()
ap.add_argument("--key", default=os.environ.get("SRNET_REGISTRY_KEY") or os.path.expanduser("~/.srnet-dev-keys/registry.key"))
ap.add_argument("--ver", default="1.0.0")
a = ap.parse_args()
kp = Path(a.key)
if not kp.exists():
    kp.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(kp, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
        f.write(os.urandom(32))
key = F.key_from_seed(kp.read_bytes())
T = ROOT / "typed" / "templates"
commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                        text=True).stdout.strip()
entries = []
for name, types in TYPES.items():
    for f in ("tk.js", "app.css"):  # every bundle carries its own pinned copy of the toolkit
        shutil.copy(T / "_common" / f, T / name / f)
    entries.append(sign_entry(key, name, a.ver, types, read_dir(T / name),
                              summary=f"{name} template {a.ver}", source_commit=commit))
(T / "registry.json").write_text(json.dumps({"pin": F.pub_hex(key), "entries": entries}, indent=1))
print("registry pin", F.pub_hex(key), "templates", len(entries))
