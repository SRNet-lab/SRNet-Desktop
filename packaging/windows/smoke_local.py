"""Smoke of the WINDOWS-SMOKE checklist against the installed client, on loopback only:
local test host -> srnet snapps -> join room by srnet:// link -> post -> mail to self ->
encrypted upload + open -> srnet ui serves Exits and Wallet. No chain transactions.
Runs on Windows (CI) and Linux/macOS (installed launcher)."""
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

WIN = os.name == "nt"
if WIN:
    base = Path(os.environ["LOCALAPPDATA"]) / "srnet-client"
    srnet = [str(base / "bin" / "srnet.cmd")]
    py = str(base / "venv" / "Scripts" / "python.exe")
else:
    base = Path.home() / ".local" / "share" / "srnet-client"
    srnet = [str(Path.home() / ".local" / "bin" / "srnet")]
    py = str(base / "venv" / "bin" / "python")
snapps = base / "app" / "snapps"
home = Path(tempfile.mkdtemp(prefix="srnet-smoke-"))
env = dict(os.environ, SRNET_HOME=str(home))
procs = []


def start(cmd, **kw):
    p = subprocess.Popen((["cmd", "/c"] if WIN and cmd[0].endswith(".cmd") else []) + cmd,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, **kw)
    procs.append(p)
    return p


def stop_all():
    for p in procs:
        if WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.terminate()


def wait_line(p, needle, n=200):
    for _ in range(n):
        line = p.stdout.readline()
        if needle in line:
            return line
        if not line and p.poll() is not None:
            break
    raise SystemExit(f"FAIL: never saw {needle!r}")


def api(path, body=None, raw=None, port=48920):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}", data=data,
                                 headers={"X-Core": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        b = r.read()
        return json.loads(b) if r.headers.get_content_type() == "application/json" else b


def wait_ports_free(ports, secs=60):
    """An earlier CI step's processes can hold the fixed ports for a moment after being killed."""
    import socket
    for _ in range(secs):
        busy = []
        for port in ports:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    busy.append(port)
        if not busy:
            return
        time.sleep(1)
    raise SystemExit(f"FAIL: ports still in use: {busy}")


try:
    wait_ports_free((48920, 48940, 48941))
    th = start([py, str(snapps / "scripts" / "local_test_host.py"), "--data", str(home / "testhost")])
    wait_line(th, "READY")
    links = json.loads((home / "testhost" / "links.json").read_text())
    print("PASS local test host", flush=True)
    sn = start(srnet + ["--home", str(home), "snapps", "--config", links["client_config"]])
    wait_line(sn, "typed client:")
    assert b"SRNet" in urllib.request.urlopen("http://127.0.0.1:48920/", timeout=10).read()
    r = subprocess.run((["cmd", "/c"] if WIN else []) + srnet + ["open", links["room_link"], "--print-only"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "/?join=" in r.stdout, r.stdout + r.stderr
    pv = api("join/preview", {"link": links["room_link"]})
    assert pv["name"] == "lobby" and pv["root"] == links["root_pin"] and pv["first_seen"], pv
    assert api("join", {"link": links["room_link"]})["joined"]
    assert api("join", {"link": links["files_link"]})["joined"]
    print("PASS join lobby + files", flush=True)
    d = api("draft", {"c": "lobby", "t": "room.msg", "body": {"text": "hello from the smoke test", "reply_to": None}})
    assert api("confirm", {"token": d["token"]}).get("ok"), d
    v = api("view/lobby")
    assert any(i["body"].get("text") == "hello from the smoke test" for i in v["view"].get("items", v["view"].get("msgs", []))) \
        or "hello from the smoke test" in json.dumps(v), v
    print("PASS post in lobby", flush=True)
    api("mail/register", {})
    me = api("mail/summary", {})["link"]
    s = api("mail/send", {"to": me, "subject": "smoke", "body": "note to self"})
    assert s.get("ok"), s
    f = api("mail/fetch", {})
    assert f["new"] == 1, f
    req = api("mail/folder", {"folder": "requests"})["msgs"]
    assert [m["subject"] for m in req] == ["smoke"], req
    print("PASS mail to self (lands in Requests)", flush=True)
    secret = b"smoke test file\n" * 40
    up = api("upload?c=files&name=smoke.txt&mime=text/plain&note=smoke&mode=gated", raw=secret)
    assert api("confirm", {"token": up["token"]}).get("ok"), up
    api("sync", {"c": "files"})
    items = api("view/files")["view"]["items"]
    fid = next(i["id"] for i in items if i.get("note") == "smoke")
    assert api("file/open", {"c": "files", "id": fid}) == secret
    print("PASS encrypted upload + open", flush=True)
    # the smoke's `srnet snapps` (now an alias of ui) already serves 48920: ui must reuse it, not start a second core
    ui = start(srnet + ["--home", str(home), "ui", "--no-browser"])
    url = wait_line(ui, "http://127.0.0.1:").split("srnet ui: ", 1)[1].split()[0]
    page = None
    for _ in range(90):
        try:
            page = urllib.request.urlopen(url, timeout=5).read().decode(); break
        except OSError:
            time.sleep(1)
    if page is None:
        ui.terminate(); print("srnet ui output:", ui.stdout.read()[-3000:], flush=True)
    assert page and 'data-b="exits"' in page and "Top up from a phone wallet" in page, page
    hdr = {"X-Core": "1", "Content-Type": "application/json"}
    col = json.loads(urllib.request.urlopen(urllib.request.Request(url + "api/collections", headers=hdr), timeout=10).read())
    assert col.get("wallet") is True, col
    ts = json.loads(urllib.request.urlopen(urllib.request.Request(url + "api/wallet/topup/status", data=b"{}", headers=hdr), timeout=10).read())
    assert ts.get("ok") is True, ts
    print("PASS srnet ui: one app, native Exits + Wallet tabs, wallet bridge answers", flush=True)
    print("SMOKE OK")
finally:
    stop_all()
