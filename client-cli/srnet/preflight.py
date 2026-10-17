"""Setup preflight ("Check my setup"): one line per check, PASS / WARN / FAIL with a fix hint.

Mirrors the Session Router Windows preflight.ps1 style: KEY=VALUE lines, plain-language FIX
lines and a final PREFLIGHT=OK|WARN|FAIL. Runs at install, at first launch, as
`srnet doctor` and from the app's Check my setup button. Read-only: it changes nothing.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import email.utils
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEMO = "srnet7fqaajaqb6u7ho56znknysg5ime6kfbaymaaih4ba3q1ndo.sesh"
SERVICE = "Session-Router"
UI_PORT = 48920
WIN = sys.platform == "win32"
VPN_WORDS = ("NordLynx", "NordVPN", "WireGuard", "Mullvad", "OpenVPN", "TAP-Windows", "ProtonVPN", "ExpressVPN",
             "Surfshark", "Windscribe", "Cloudflare WARP", "ZeroTier", "Tailscale", "AnyConnect", "GlobalProtect")


def _ps(cmd: str, timeout=20) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                           text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _app_dir() -> Path | None:
    p = Path(sys.executable).resolve().parent.parent  # <app>\python\python.exe
    return p if (p / "router" / "session-router.exe").exists() else None


def _listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def c_os():
    if not WIN:
        return "SKIP", f"{platform.system()} {platform.release()} {platform.machine()} (Windows-only checks skipped)", ""
    v = sys.getwindowsversion()
    arch = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    d = f"Windows {v.major}.{v.minor} build {v.build} {arch}"
    if arch.upper() != "AMD64":
        return "FAIL", d, "SRNet needs 64-bit x64 Windows; ARM64 and 32-bit are not supported yet"
    if v.major < 10 or v.build < 17763:
        return "FAIL", d, "update to Windows 10 1809 or newer (Windows 11 is fine)"
    return "PASS", d, ""


def c_admin():
    if not WIN:
        return "SKIP", "not Windows", ""
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    svc = _ps(f"(Get-Service '{SERVICE}' -ErrorAction SilentlyContinue).Status")
    if svc:
        return "PASS", f"service installed (by an administrator); this user admin={admin}", ""
    return ("WARN" if admin else "FAIL"), f"service not installed; admin={admin}", \
        "re-run the SRNet installer and accept the administrator (UAC) prompt"


def c_wintun():
    if not WIN:
        return "SKIP", "not Windows", ""
    app = _app_dir()
    dll = app / "router" / "wintun.dll" if app else None
    if not dll or not dll.exists():
        return "FAIL", "wintun.dll missing", "reinstall SRNet; antivirus may have quarantined wintun.dll"
    try:
        ctypes.WinDLL(str(dll))
    except OSError as e:
        return "FAIL", f"wintun.dll won't load: {e}", "reinstall SRNet; check antivirus quarantine"
    sig = _ps(f"(Get-AuthenticodeSignature '{dll}').Status")
    up = _ps("(Get-NetAdapter -Name 'sr-tun0' -ErrorAction SilentlyContinue).Status")
    if not up:
        return "WARN", f"driver loads (signature {sig}); adapter sr-tun0 not present", \
            "start the router (Start menu > SRNet); if it stays missing, reboot once"
    return "PASS", f"driver loads (signature {sig}); sr-tun0 {up}", ""


def c_ports():
    if not WIN:
        if not _listening(UI_PORT):
            return "PASS", f"tcp {UI_PORT} free", ""
        try:
            who = subprocess.run(["ss", "-Hltnp", f"sport = :{UI_PORT}"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            who = ""
        pid = who.split("pid=")[1].split(",")[0] if "pid=" in who else ""
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode() if pid else ""
        except OSError:
            cmd = ""
        if "typed.core" in cmd:
            return "PASS", f"tcp {UI_PORT} is the SRNet app (already running, pid {pid})", ""
        return "WARN", f"tcp {UI_PORT} in use" + (f" by pid {pid}" if pid else ""), \
            f"another program holds the app port {UI_PORT}; close it or run srnet ui --port N"
    out = _ps("Get-NetTCPConnection -State Listen -LocalPort 48920,1190 -ErrorAction SilentlyContinue | "
              "ForEach-Object { \"tcp:$($_.LocalAddress):$($_.LocalPort):$((Get-Process -Id $_.OwningProcess).ProcessName)\" }; "
              "Get-NetUDPEndpoint -LocalPort 53,1191 -ErrorAction SilentlyContinue | "
              "ForEach-Object { \"udp:$($_.LocalAddress):$($_.LocalPort):$((Get-Process -Id $_.OwningProcess).ProcessName)\" }")
    fail, warn = [], []
    for x in sorted(set(out.split())):
        proto, rest = x.split(":", 1)
        addr, port, name = rest.rsplit(":", 2)
        if name.lower() in ("python", "pythonw", "session-router"):
            continue
        # only a bind on loopback or the wildcard collides with the router (127.0.0.1) / client
        (fail if addr in ("127.0.0.1", "0.0.0.0", "::", "::1") else warn).append(f"{proto} {addr}:{port} {name}")
    d = " ".join(sorted(set(out.split()))) or "all free"
    if fail:
        return "FAIL", d, "another program holds " + ", ".join(fail) + "; close it (Task Manager) or stop the other DNS/VPN service"
    if warn:
        return "WARN", d, "other programs use " + ", ".join(warn) + " on other addresses; fine unless .sesh lookups fail"
    return "PASS", d, ""


def c_router():
    st = _ps(f"(Get-Service '{SERVICE}' -ErrorAction SilentlyContinue).Status") if WIN else ""
    if WIN and st != "Running":
        return "FAIL", f"service {st or 'missing'}", "Start menu > SRNet starts it; or in an admin PowerShell: Start-Service Session-Router"
    try:
        from .rpc import Rpc
        r = Rpc("tcp://127.0.0.1:1190", timeout_ms=4000).call("llarp.status")
        res = r.get("result", r) if isinstance(r, dict) else {}
        paths = next((res.get(k) for k in ("numPathsBuilt", "paths", "numPaths") if isinstance(res, dict) and k in res), None)
        return "PASS", f"service {st or '-'}; API answers" + (f"; paths={paths}" if paths is not None else ""), ""
    except Exception as e:  # noqa: BLE001
        return "WARN", f"service {st or '-'}; API 127.0.0.1:1190 no answer ({str(e)[:80]})", \
            ("give it a minute after start; if it persists see C:\\ProgramData\\Session-Router\\session-router.log" if WIN
             else "start session-router (API on 127.0.0.1:1190) and give it a minute")


def _linux_dns_hint() -> str:
    """Why Linux .sesh lookups fail, from resolv.conf and resolvectl (read-only)."""
    try:
        rc = os.path.realpath("/etc/resolv.conf")
        txt = open("/etc/resolv.conf", encoding="utf-8", errors="replace").read()
    except OSError:
        rc, txt = "", ""
    if "127.0.0.53" not in txt and "/run/systemd/resolve/" not in rc:
        return ("/etc/resolv.conf does not use systemd-resolved, so .sesh never reaches Session Router: "
                "sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf (README step 1)")
    try:
        out = subprocess.run(["resolvectl", "status", "sr-tun0"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    if "127.3.2.1" not in out or "~sesh" not in out:
        return ("sr-tun0 has no .sesh DNS: add the session-router-client dns.conf drop-in from README step 1, "
                "then sudo systemctl daemon-reload && sudo systemctl restart session-router-client")
    return "resolvectl looks right; check that session-router-client is running (systemctl status session-router-client)"

def c_dns():
    try:
        infos = socket.getaddrinfo("localhost.sesh", 80, proto=socket.IPPROTO_TCP)
        return "PASS", "localhost.sesh -> " + infos[0][4][0], ""
    except OSError as e:
        if WIN:
            rule = _ps("(Get-DnsClientNrptRule | Where-Object Comment -eq 'SRNet').Namespace")
            return "FAIL", f"no .sesh lookup ({e}); NRPT rule: {rule or 'missing'}", \
                "reinstall SRNet (it adds the .sesh DNS rule); a VPN or DNS filter may be overriding it"
        if sys.platform == "darwin":
            return "FAIL", f"no .sesh lookup ({e})", \
                "point .sesh lookups at 127.3.2.1 (see the Session Router install guide)"
        return "FAIL", f"no .sesh lookup ({e})", _linux_dns_hint()


def c_demo():
    url = f"http://{DEMO}/c/lobby/v1/info"
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            r.read(4096)
        return "PASS", f"demo lobby answered in {time.time() - t:.1f}s", ""
    except OSError as e:
        return "WARN", f"demo not reached ({str(e)[:80]})", \
            "first paths can take 1-3 minutes; networks that block outbound UDP stop the router"


def c_clock():
    last = None
    for url in ("https://www.cloudflare.com/", "https://www.google.com/", "https://www.microsoft.com/"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=6) as r:
                remote = email.utils.parsedate_to_datetime(r.headers["Date"])
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        return "WARN", f"couldn't compare the clock ({str(last)[:60]})", "check Settings > Time & language > Sync now"
    skew = abs((_dt.datetime.now(_dt.timezone.utc) - remote).total_seconds())
    if skew > 300:
        return "FAIL", f"clock off by {skew:.0f}s", "Settings > Time & language > Sync now (signed records and the router need a correct clock)"
    return ("WARN" if skew > 60 else "PASS"), f"clock skew {skew:.0f}s", "Settings > Time & language > Sync now" if skew > 60 else ""


def c_disk():
    free = shutil.disk_usage(Path(sys.executable).anchor or "/").free / 2**30
    if free < 0.5:
        return "FAIL", f"{free:.2f} GiB free", "free up at least 1 GiB on the system drive"
    return ("WARN" if free < 2 else "PASS"), f"{free:.1f} GiB free", "free up some space" if free < 2 else ""


def c_block():
    if not WIN:
        return "SKIP", "not Windows", ""
    hits = []
    app = _app_dir()
    if app:
        fw = _ps(f"Get-NetFirewallApplicationFilter -Program '{app}\\router\\session-router.exe' -ErrorAction SilentlyContinue | "
                 "Get-NetFirewallRule | Where-Object {{$_.Action -eq 'Block' -and $_.Enabled -eq 'True'}} | ForEach-Object DisplayName")
        if fw:
            hits.append("firewall block rule: " + fw.replace("\n", "; "))
    av = _ps("Get-MpThreatDetection -ErrorAction SilentlyContinue | ForEach-Object { $_.Resources } | "
             "Where-Object { $_ -match 'SRNet|session-router|wintun' }")
    if av:
        hits.append("Defender detections: " + av.replace("\n", "; ")[:200])
    vpn = _ps("Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { \"$($_.Name) $($_.InterfaceDescription)\" }")
    for line in vpn.splitlines():
        if not line.lower().startswith("sr-tun") and any(w.lower() in line.lower() for w in VPN_WORDS):
            hits.append("VPN adapter up: " + line.strip())
    if hits:
        return "WARN", " | ".join(hits), "turn off the VPN (quit it fully, e.g. in Task Manager) and/or add a firewall exception for SRNet, then restart SRNet"
    return "PASS", "no blocking rule, detection or VPN seen", ""


CHECKS = [("os", c_os), ("admin", c_admin), ("wintun", c_wintun), ("ports", c_ports), ("router", c_router),
          ("dns", c_dns), ("demo", c_demo), ("clock", c_clock), ("disk", c_disk), ("firewall_av", c_block)]


def run(skip=(), only=None) -> dict:
    rows = []
    for name, fn in CHECKS:
        if name in skip or (only and name not in only):
            continue
        fn = globals().get(getattr(fn, "__name__", ""), fn)  # late-bound (patchable in tests)
        try:
            status, detail, fix = fn()
        except Exception as e:  # noqa: BLE001  a broken check is a warning, never a crash
            status, detail, fix = "WARN", f"check error: {str(e)[:120]}", ""
        rows.append({"check": name, "status": status, "detail": detail, "fix": fix})
    worst = "FAIL" if any(r["status"] == "FAIL" for r in rows) else "WARN" if any(r["status"] == "WARN" for r in rows) else "OK"
    return {"preflight": worst, "when": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "host": platform.node(), "checks": rows}


def text(rep: dict) -> str:
    out = []
    for r in rep["checks"]:
        out.append(f"CHECK {r['check']}={r['status']} {r['detail']}")
        if r["fix"] and r["status"] != "PASS":
            out.append(f"FIX: {r['fix']}")
    out += [f"WHEN={rep['when']}", f"PREFLIGHT={rep['preflight']}"]
    return "\n".join(out) + "\n"


def default_report_path(home: Path | None = None) -> Path:
    if WIN and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SRNet" / "preflight-report.txt"
    return Path(home or os.environ.get("SRNET_HOME") or Path.home() / ".srnet") / "preflight-report.txt"


def write_report(rep: dict, path: Path | None = None, home: Path | None = None) -> Path:
    path = Path(path or default_report_path(home))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text(rep) + "\n" + json.dumps(rep, indent=1) + "\n", encoding="utf-8")
    return path
