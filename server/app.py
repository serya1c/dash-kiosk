#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Геромес Dashboard v2 — домашняя инфраструктура на одном экране.

Контейнер: network_mode=host, pid=host, /:/host:ro, docker.sock, SSH-ключ ПК.
Коллекторы пишут снимок в STATE; /api/all отдаёт его браузеру.
Действия (WoL, ComfyUI, контейнеры, выбор ОС) — очередь в фоновом воркере.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import mimetypes
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DASH_PORT", "8090"))
ROOT = os.path.dirname(os.path.abspath(__file__))
HOST_ROOT = "/host"
DATA_DIR = os.environ.get("DASH_DATA", "/app/data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

CONFIG_PATH = os.environ.get("DASH_CONFIG", "/app/config.json")


def _default_config() -> dict:
    return {
        "server": {"name": "Дом", "subtitle": "домашняя инфраструктура"},
        "windows": {
            "enabled": False,
            "host": "",
            "user": "",
            "key": "/keys/win",
            "mac": "",
            "output": "",
            "comfy_task": "ComfyUI",
        },
        "weather": {"city": "", "lat": 0.0, "lon": 0.0},
        "services": [],
        "linux_hosts": [],
        "docker_actions": [],
        "comfy": {"enabled": False, "local_port": 18188, "remote_port": 8188},
        "demo": False,
    }


def load_config() -> dict:
    """config.json → env-переопределения. Отсутствующий файл не ошибка."""
    cfg = _default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        print("config:", exc, flush=True)

    win = cfg["windows"]
    win["host"] = os.environ.get("WIN_HOST", win.get("host") or "")
    win["user"] = os.environ.get("WIN_USER", win.get("user") or "")
    win["key"] = os.environ.get("WIN_KEY", win.get("key") or "/keys/win")
    win["output"] = os.environ.get("WIN_OUTPUT", win.get("output") or "")
    win["mac"] = os.environ.get("WIN_MAC", win.get("mac") or "")
    win["comfy_task"] = os.environ.get("COMFY_TASK", win.get("comfy_task") or "ComfyUI")
    if win["host"] and win["user"]:
        win["enabled"] = bool(win.get("enabled", True))
    cfg["demo"] = bool(cfg.get("demo")) or os.environ.get("DEMO", "").lower() in ("1", "true", "yes")
    return cfg


CONFIG = load_config()
DEMO = CONFIG["demo"]
SERVER_NAME = (CONFIG.get("server") or {}).get("name") or "Дом"
SERVER_SUBTITLE = (CONFIG.get("server") or {}).get("subtitle") or ""

WIN_USER = CONFIG["windows"]["user"]
WIN_HOST = CONFIG["windows"]["host"]
WIN_KEY = CONFIG["windows"]["key"]
WIN_MAC = CONFIG["windows"]["mac"]
WIN_OUTPUT = CONFIG["windows"]["output"]
COMFY_TASK = CONFIG["windows"]["comfy_task"]
WINDOWS_ENABLED = bool(CONFIG["windows"].get("enabled"))


# EFI-записи (проверено bcdedit /enum firmware)
OS_ENTRIES = {
    "windows": "{bootmgr}",
    "opensuse": "{0e5c6c43-0b09-11ef-b566-806e6f6e6963}",
}

SSH_BASE = [
    "ssh", "-i", WIN_KEY,
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=15",
]

STATE: dict = {}
LOCK = threading.Lock()
HIST = {k: deque(maxlen=300) for k in ("pc_cpu", "pc_gpu", "pc_ram", "pc_vram", "host_cpu", "host_ram")}
ACTION_Q: "queue.Queue[dict]" = queue.Queue()


def put(key: str, value) -> None:
    with LOCK:
        STATE[key] = value


def get(key: str, default=None):
    with LOCK:
        return STATE.get(key, default)


CURRENT_JOB: dict = {"id": None, "name": None}


def note(detail: str) -> None:
    """Промежуточный статус долгого действия — его видно в баннере «Действие»."""
    if not CURRENT_JOB.get("id"):
        return
    put("action", {"id": CURRENT_JOB["id"], "name": CURRENT_JOB.get("name"),
                   "state": "running", "detail": detail, "ts": time.time()})


def hist_push(key: str, value) -> None:
    try:
        HIST[key].append(round(float(value), 1))
    except (TypeError, ValueError):
        pass


def rf(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def http_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


# --------------------------------------------------------------- settings ---

def load_settings() -> dict:
    defaults = {"default_os": "windows", "wol_mac": WIN_MAC or ""}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        defaults.update({k: v for k, v in data.items() if k in defaults})
    except Exception:  # noqa: BLE001
        pass
    return defaults


def save_settings(data: dict) -> dict:
    cur = load_settings()
    for k in ("default_os", "wol_mac"):
        if k in data:
            cur[k] = data[k]
    if cur["default_os"] not in OS_ENTRIES:
        cur["default_os"] = "windows"
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, ensure_ascii=False, indent=2)
    put("settings", cur)
    return cur


# ---------------------------------------------------------------- HASTRII ---

_cpu_prev: dict = {}
_cpu_lock = threading.Lock()


def cpu_sampler() -> dict:
    cores, total_pct = [], 0.0
    lines = [ln for ln in rf("/proc/stat").splitlines() if ln.startswith("cpu")]
    with _cpu_lock:
        for ln in lines:
            parts = ln.split()
            name = parts[0]
            vals = [float(v) for v in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
            total = sum(vals)
            prev = _cpu_prev.get(name)
            pct = 0.0
            if prev and total > prev[0]:
                dt = total - prev[0]
                di = idle - prev[1]
                pct = max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))
            _cpu_prev[name] = (total, idle)
            if name == "cpu":
                total_pct = pct
            else:
                cores.append(round(pct, 1))
    return {"cpu": round(total_pct, 1), "cores": cores}


def mem_info() -> dict:
    info = {}
    for ln in rf("/proc/meminfo").splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            info[k] = float(v.strip().split()[0]) if v.strip() else 0.0
    total = info.get("MemTotal", 1) * 1024
    avail = info.get("MemAvailable", 0) * 1024
    stotal = info.get("SwapTotal", 0) * 1024
    sfree = info.get("SwapFree", 0) * 1024
    return {"total": total, "used": total - avail, "avail": avail,
            "swap_total": stotal, "swap_used": stotal - sfree}


def load_avg() -> list:
    try:
        return [round(float(x), 2) for x in rf("/proc/loadavg").split()[:3]]
    except ValueError:
        return [0, 0, 0]


def uptime_s() -> int:
    try:
        return int(float(rf("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        return 0


FS_OK = {"ext4", "ext3", "ext2", "xfs", "btrfs", "vfat", "fuseblk", "ntfs", "ntfs3", "exfat", "zfs", "f2fs"}


def disk_list() -> list:
    out, seen_mp = [], set()
    raw_mounts = rf("/proc/1/mounts") or rf("/proc/mounts")
    for ln in raw_mounts.splitlines():
        parts = ln.split()
        if len(parts) < 3:
            continue
        dev, mp, fst = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fst not in FS_OK or mp in seen_mp:
            continue
        if mp.startswith(("/var/lib/docker", "/snap", "/boot")):
            continue
        try:
            st = os.statvfs(HOST_ROOT + mp if mp != "/" else HOST_ROOT)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total < 200 * 1024 * 1024:
            continue
        seen_mp.add(mp)
        out.append({"mount": mp, "dev": os.path.basename(dev), "fs": fst,
                    "total": total, "used": total - free, "free": free})
    out.sort(key=lambda d: d["mount"] != "/data")
    return out


_net_prev: dict = {}


def net_rates() -> dict:
    rx = tx = 0
    for ln in rf("/proc/net/dev").splitlines()[2:]:
        if ":" not in ln:
            continue
        iface, data = ln.split(":", 1)
        iface = iface.strip()
        if iface == "lo" or iface.startswith(("docker", "br-", "veth", "virbr", "tun", "tap")):
            continue
        f = data.split()
        try:
            rx += int(f[0])
            tx += int(f[8])
        except (IndexError, ValueError):
            continue
    now = time.time()
    prev = _net_prev.get("v")
    rate = (0.0, 0.0)
    if prev:
        dt = now - prev[2]
        if dt > 0.2:
            rate = (max(0.0, (rx - prev[0]) / dt), max(0.0, (tx - prev[1]) / dt))
    _net_prev["v"] = (rx, tx, now)
    return {"rx": rate[0], "tx": rate[1], "total_rx": rx, "total_tx": tx}


def temps() -> list:
    res = []
    for path in glob.glob(HOST_ROOT + "/sys/class/hwmon/hwmon*/temp*_input"):
        raw = rf(path).strip()
        if not raw:
            continue
        try:
            val = int(raw) / 1000.0
        except ValueError:
            continue
        if not (5 < val < 120):
            continue
        base = path[:-6]
        label = rf(base + "_label").strip() or rf(os.path.dirname(path) + "/name").strip()
        res.append({"name": label or "sensor", "c": round(val, 1)})
    res.sort(key=lambda x: -x["c"])
    seen, uniq = set(), []
    for t in res:
        if t["name"] in seen:
            continue
        seen.add(t["name"])
        uniq.append(t)
    return uniq[:6]


def docker_ps() -> dict:
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "items": []}
    items = []
    for ln in r.stdout.splitlines():
        f = ln.split("\t")
        if len(f) >= 4:
            items.append({"name": f[0], "state": f[1], "status": f[2], "image": f[3]})
    items.sort(key=lambda i: (i["state"] != "running", i["name"]))
    return {"ok": True, "items": items,
            "running": sum(1 for i in items if i["state"] == "running"), "total": len(items)}


def host_loop() -> None:
    while True:
        try:
            cpu = cpu_sampler()
            mem = mem_info()
            put("host", {
                "cpu": cpu, "mem": mem, "load": load_avg(), "uptime": uptime_s(),
                "disks": disk_list(), "net": net_rates(), "temps": temps(),
            })
            hist_push("host_cpu", cpu["cpu"])
            hist_push("host_ram", (mem["used"] / mem["total"] * 100.0) if mem["total"] else 0)
        except Exception as exc:  # noqa: BLE001
            print("host_loop:", exc, flush=True)
        time.sleep(4)


# ------------------------------------------------------------------ Windows ---

WIN_PS = r"""
$ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
function PortUp([int]$p){
  try{ $c=New-Object Net.Sockets.TcpClient; $r=$c.BeginConnect('127.0.0.1',$p,$null,$null)
    $ok=$r.AsyncWaitHandle.WaitOne(200); $up=$ok -and $c.Connected; $c.Close(); return $up }catch{return $false}
}
$null = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
while ($true) {
  $o = Get-CimInstance Win32_OperatingSystem
  $cpu = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime
  $gpu = & nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,fan.speed --format=csv,noheader,nounits 2>$null
  $disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    @{ name=$_.DeviceID; total=[int64]$_.Size; free=[int64]$_.FreeSpace } }
  $all = Get-Process
  $procs = $all | Sort-Object -Property WorkingSet64 -Descending | Select-Object -First 5 |
    ForEach-Object { @{ name=$_.ProcessName; mb=[int]($_.WorkingSet64/1MB) } }
  $cprocs = $all | Sort-Object -Property CPU -Descending | Select-Object -First 5 |
    ForEach-Object { @{ name=$_.ProcessName; cpu=[math]::Round([double]$_.CPU,0) } }
  $out = @{
    cpu = [int]$cpu
    ram_total = [int64]$o.TotalVisibleMemorySize * 1024
    ram_free = [int64]$o.FreePhysicalMemory * 1024
    uptime = [int64]((Get-Date) - $o.LastBootUpTime).TotalSeconds
    gpu_raw = if ($gpu) { ($gpu -join "`n") } else { "" }
    disks = @($disks)
    procs_ram = @($procs)
    procs_cpu = @($cprocs)
    comfy_port = PortUp 8188
    ollama_port = PortUp 11434
  }
  [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress -Depth 5))
  [Console]::Out.Flush()
  Start-Sleep -Milliseconds 3000
}
"""

WIN_PS_FILES = r"""
$ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$root = R"OUTPUT"
Get-ChildItem $root -File -Recurse -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 10 |
  ForEach-Object { @{ rel=$_.FullName.Substring($root.Length+1).Replace('\','/'); mb=[math]::Round($_.Length/1MB,2); ext=$_.Extension } } |
  ConvertTo-Json -Compress -Depth 5
"""


def ps_encoded(script: str) -> str:
    return base64.b64encode(script.replace("\n", "\r\n").encode("utf-16-le")).decode()


def ssh_run(script: str, timeout: int = 40) -> str:
    cmd = SSH_BASE + [f"{WIN_USER}@{WIN_HOST}", "powershell", "-NoProfile",
                      "-NonInteractive", "-EncodedCommand", ps_encoded(script)]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", "replace").replace("#< CLIXML", "").strip()


def parse_gpu(raw: str) -> list:
    res = []
    for ln in (raw or "").splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) < 5:
            continue

        def fl(i, d=0.0):
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return d

        res.append({"name": f[0], "util": fl(1), "vram_used": fl(2), "vram_total": fl(3),
                    "temp": fl(4), "power": fl(5), "fan": fl(6)})
    return res


def build_win(j: dict) -> dict:
    return {
        "online": True,
        "cpu": int(j.get("cpu") or 0),
        "ram_total": int(j.get("ram_total") or 0),
        "ram_free": int(j.get("ram_free") or 0),
        "uptime": int(j.get("uptime") or 0),
        "gpus": parse_gpu(j.get("gpu_raw") or ""),
        "disks": [{"mount": d.get("name"), "total": int(d.get("total") or 0),
                   "free": int(d.get("free") or 0)} for d in (j.get("disks") or [])],
        "procs_ram": j.get("procs_ram") or [],
        "procs_cpu": j.get("procs_cpu") or [],
        "comfy_local": bool(j.get("comfy_port")),
        "ollama_local": bool(j.get("ollama_port")),
    }


def win_loop() -> None:
    """Один долгий SSH-сеанс: PowerShell печатает JSON каждые 3 с."""
    import select
    while True:
        cmd = SSH_BASE + [f"{WIN_USER}@{WIN_HOST}", "powershell", "-NoProfile",
                          "-NonInteractive", "-EncodedCommand", ps_encoded(WIN_PS)]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
            while True:
                ready, _, _ = select.select([proc.stdout], [], [], 25)
                if not ready:
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue
                try:
                    data = build_win(json.loads(text))
                except Exception:  # noqa: BLE001
                    continue
                put("win", data)
                hist_push("pc_cpu", data["cpu"])
                if data["ram_total"]:
                    hist_push("pc_ram", (data["ram_total"] - data["ram_free"]) / data["ram_total"] * 100)
                if data["gpus"]:
                    g0 = data["gpus"][0]
                    hist_push("pc_gpu", g0.get("util") or 0)
                    if g0.get("vram_total"):
                        hist_push("pc_vram", (g0.get("vram_used") or 0) / g0["vram_total"] * 100)
        except Exception as exc:  # noqa: BLE001
            put("win", {"online": False, "error": str(exc)[:150]})
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        put("win", {"online": False, "error": "нет ответа по SSH"})
        time.sleep(3)


def win_files_loop() -> None:
    script = WIN_PS_FILES.replace('R"OUTPUT"', "'" + WIN_OUTPUT + "'")
    while True:
        try:
            raw = ssh_run(script, timeout=40)
            start = raw.find("[")
            if start < 0:
                start = raw.find("{")
            if start >= 0:
                j = json.loads(raw[start:])
                if isinstance(j, dict):
                    j = [j]
                put("files", j[:10])
        except Exception as exc:  # noqa: BLE001
            print("files:", exc, flush=True)
        time.sleep(30)


# ------------------------------------------------------------------- Comfy ---

COMFY_LOCAL = "http://127.0.0.1:18188"
OLLAMA_LOCAL = "http://127.0.0.1:18134"


def comfy_loop() -> None:
    while True:
        stats = http_json(COMFY_LOCAL + "/system_stats", timeout=3)
        queue_ = http_json(COMFY_LOCAL + "/queue", timeout=3)
        info = {"online": bool(stats)}
        if stats:
            d0 = (stats.get("devices") or [{}])[0]
            sysinfo = stats.get("system") or {}
            info.update({
                "vram_total": d0.get("vram_total"), "vram_free": d0.get("vram_free"),
                "device": d0.get("name"),
                "comfy_version": sysinfo.get("comfyui_version"),
                "py": (sysinfo.get("python_version") or "")[:12],
            })
        if queue_:
            running = queue_.get("queue_running") or []
            pending = queue_.get("queue_pending") or []
            info["running"] = len(running)
            info["pending"] = len(pending)
            if running:
                info["running_id"] = str(running[0][1])[:8] if len(running[0]) > 1 else ""
        put("comfy", info)
        time.sleep(5)


def ollama_loop() -> None:
    while True:
        tags = http_json(OLLAMA_LOCAL + "/api/tags", timeout=3)
        models = []
        if tags:
            for m in tags.get("models") or []:
                models.append({"name": m.get("name"), "size": m.get("size")})
        put("ollama", {"online": bool(tags), "models": models[:8]})
        time.sleep(30)


def tunnel_loop() -> None:
    while True:
        cmd = SSH_BASE + [
            "-N", "-o", "ExitOnForwardFailure=yes",
            "-L", "127.0.0.1:18188:127.0.0.1:8188",
            "-L", "127.0.0.1:18134:127.0.0.1:11434",
            f"{WIN_USER}@{WIN_HOST}",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.wait()
        except Exception as exc:  # noqa: BLE001
            print("tunnel:", exc, flush=True)
        time.sleep(5)


# ----------------------------------------------------------------- services ---

def _service_list() -> list:
    items = []
    for svc in CONFIG.get("services") or []:
        if isinstance(svc, str):
            items.append((svc, svc, "http"))
        elif isinstance(svc, dict) and svc.get("url"):
            items.append((svc.get("name") or svc["url"], svc["url"], "http"))
    return items


LOCAL_SERVICES = _service_list()


def check_one(item):
    name, url, _kind = item
    t0 = time.time()

    def result(code, state):
        return {"name": name, "url": url, "code": code, "ok": state != "down",
                "state": state, "ms": int((time.time() - t0) * 1000)}

    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hermes-dash/2.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            code = resp.status
        return result(code, "ok" if code < 400 else ("auth" if code in (401, 403) else "down"))
    except urllib.error.HTTPError as exc:
        code = exc.code
        return result(code, "ok" if code < 400 else ("auth" if code in (401, 403) else "down"))
    except Exception as exc:  # noqa: BLE001
        out = result(0, "down")
        out["error"] = str(exc)[:80]
        return out


def services_loop() -> None:
    pool = ThreadPoolExecutor(max_workers=8)
    while True:
        try:
            put("services", list(pool.map(check_one, LOCAL_SERVICES)))
            put("docker", docker_ps())
        except Exception as exc:  # noqa: BLE001
            print("services:", exc, flush=True)
        time.sleep(15)


# ------------------------------------------------------------------ weather ---

WEATHER_CODES = {
    0: ("Ясно", "sun"), 1: ("Малооблачно", "sun"), 2: ("Переменная облачность", "cloud"),
    3: ("Пасмурно", "cloud"), 45: ("Туман", "fog"), 48: ("Изморозь", "fog"),
    51: ("Морось", "rain"), 53: ("Морось", "rain"), 55: ("Морось", "rain"),
    56: ("Ледяная морось", "rain"), 57: ("Ледяная морось", "rain"),
    61: ("Дождь", "rain"), 63: ("Дождь", "rain"), 65: ("Сильный дождь", "rain"),
    66: ("Ледяной дождь", "rain"), 67: ("Ледяной дождь", "rain"),
    71: ("Снег", "snow"), 73: ("Снег", "snow"), 75: ("Сильный снег", "snow"),
    77: ("Снежная крупа", "snow"), 80: ("Ливень", "rain"), 81: ("Ливень", "rain"),
    82: ("Сильный ливень", "rain"), 85: ("Снегопад", "snow"), 86: ("Снегопад", "snow"),
    95: ("Гроза", "storm"), 96: ("Гроза с градом", "storm"), 99: ("Гроза с градом", "storm"),
}

WEATHER_CFG = CONFIG.get("weather") or {}
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,"
    "relative_humidity_2m&daily=weather_code,temperature_2m_max,temperature_2m_min"
    "&timezone=auto&forecast_days=3"
).format(lat=WEATHER_CFG.get("lat", 0), lon=WEATHER_CFG.get("lon", 0))
WEATHER_CITY = WEATHER_CFG.get("city") or "погода"


def weather_loop() -> None:
    while True:
        try:
            j = http_json(WEATHER_URL, timeout=10)
            if j:
                cur = j.get("current") or {}
                daily = j.get("daily") or {}
                code = int(cur.get("weather_code") or 0)
                desc, kind = WEATHER_CODES.get(code, ("—", "cloud"))
                days = []
                for i, day in enumerate(daily.get("time") or []):
                    dc = int((daily.get("weather_code") or [0])[i] or 0)
                    days.append({"date": day,
                                 "min": round((daily.get("temperature_2m_min") or [0])[i]),
                                 "max": round((daily.get("temperature_2m_max") or [0])[i]),
                                 "desc": WEATHER_CODES.get(dc, ("—", "cloud"))[0],
                                 "kind": WEATHER_CODES.get(dc, ("—", "cloud"))[1]})
                put("weather", {"ok": True, "city": WEATHER_CITY,
                                "temp": round(cur.get("temperature_2m") or 0, 1),
                                "feels": round(cur.get("apparent_temperature") or 0, 1),
                                "desc": desc, "kind": kind,
                                "wind": round(cur.get("wind_speed_10m") or 0),
                                "hum": round(cur.get("relative_humidity_2m") or 0),
                                "days": days})
        except Exception as exc:  # noqa: BLE001
            print("weather:", exc, flush=True)
        time.sleep(900 if get("weather") else 25)


# -------------------------------------------------------------------- media ---

MEDIA_CACHE_DIR = "/tmp/dashmedia"
MEDIA_TTL = 900
_media_lock = threading.Lock()
_media_meta: dict = {}


def safe_rel(rel: str) -> str:
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or not parts:
        raise ValueError("bad path")
    return "/".join(parts)


def fetch_media(rel: str) -> tuple[str, str]:
    rel = safe_rel(rel)
    win_path = WIN_OUTPUT.replace("\\", "/").rstrip("/") + "/" + rel
    digest = hashlib.sha1(win_path.encode()).hexdigest()[:16]
    ext = os.path.splitext(rel)[1].lower() or ".bin"
    dest = os.path.join(MEDIA_CACHE_DIR, digest + ext)
    os.makedirs(MEDIA_CACHE_DIR, exist_ok=True)
    with _media_lock:
        meta = _media_meta.get(dest)
        if meta and os.path.exists(dest) and (time.time() - meta["ts"]) < MEDIA_TTL:
            return dest, meta["type"]
    cmd = ["sftp", "-q", "-b", "-", "-i", WIN_KEY,
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR", f"{WIN_USER}@{WIN_HOST}"]
    ok = False
    for remote in ("/" + win_path, win_path):
        r = subprocess.run(cmd, input=f'get "{remote}" "{dest}"\n'.encode(),
                           capture_output=True, timeout=90)
        if r.returncode == 0 and os.path.exists(dest):
            ok = True
            break
    if not ok:
        raise FileNotFoundError("sftp get failed")
    ctype = mimetypes.guess_type(dest)[0] or "application/octet-stream"
    with _media_lock:
        _media_meta[dest] = {"ts": time.time(), "type": ctype}
    return dest, ctype


# -------------------------------------------------------------- linux hosts ---

LINUX_HOSTS = CONFIG.get("linux_hosts") or []

LINUX_PROBE = r"""
LC_ALL=C
read -r _ u n sy i w irq sirq st _ < /proc/stat
t1=$((u+n+sy+i+w+irq+sirq+st)); i1=$((i+w))
sleep 0.4
read -r _ u n sy i w irq sirq st _ < /proc/stat
t2=$((u+n+sy+i+w+irq+sirq+st)); i2=$((i+w))
if [ "$t2" -gt "$t1" ]; then echo "CPU|$(( (t2-t1-(i2-i1))*100/(t2-t1) ))"; else echo "CPU|0"; fi
awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{if(t>0) print "MEM|"t*1024"|"(t-a)*1024}' /proc/meminfo
echo "LOAD|$(cut -d" " -f1-3 /proc/loadavg | tr " " "|")"
echo "UP|$(cut -d. -f1 /proc/uptime)"
df -B1 --output=source,target,used,size -x tmpfs -x devtmpfs -x efivarfs -x squashfs -x overlay 2>/dev/null |
  awk 'NR>1 && !seen[$1]++ {print "DISK|"$2"|"$3"|"$4}'
nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null |
  awk -F", " '{printf "GPU|%s|%s|%s|%s|%s|%s\n", $1,$2,$3,$4,$5,$6}'
docker ps --format "{{.Names}}|{{.Status}}" 2>/dev/null | awk -F"|" '{print "DCK|"$1"|"$2}'
for f in /sys/class/hwmon/hwmon*/temp*_input; do
  [ -r "$f" ] || continue
  v=$(cat "$f" 2>/dev/null) || continue
  [ "$v" -gt 5000 ] && [ "$v" -lt 120000 ] || continue
  lbl=$(cat "${f%_input}_label" 2>/dev/null || basename "$(dirname "$f")")
  echo "TEMP|$lbl|$((v/1000))"
done | awk -F"|" '!seen[$2]++ {print}' | head -4
"""


def parse_linux(host: dict, out: str) -> dict:
    res = {"name": host.get("name") or host.get("host"), "online": True,
           "disks": [], "temps": [], "docker": []}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2:
            continue
        key = parts[0]
        try:
            if key == "CPU":
                res["cpu"] = float(parts[1])
            elif key == "MEM":
                res["mem_total"] = int(parts[1]); res["mem_used"] = int(parts[2])
            elif key == "LOAD":
                res["load"] = [float(x) for x in parts[1:4]]
            elif key == "UP":
                res["uptime"] = int(parts[1])
            elif key == "DISK" and len(parts) >= 4:
                res["disks"].append({"mount": parts[1], "used": int(parts[2]), "total": int(parts[3])})
            elif key == "GPU" and len(parts) >= 7:
                res["gpu"] = {"name": parts[1], "util": float(parts[2]), "vram_used": float(parts[3]),
                              "vram_total": float(parts[4]), "temp": float(parts[5]), "power": float(parts[6])}
            elif key == "DCK" and len(parts) >= 3:
                res["docker"].append({"name": parts[1], "status": parts[2]})
            elif key == "TEMP" and len(parts) >= 3:
                res["temps"].append({"name": parts[1], "c": float(parts[2])})
        except (ValueError, IndexError):
            continue
    return res


def probe_linux(host: dict) -> dict:
    cmd = ["ssh", "-i", host.get("key") or "/keys/opensuse",
           "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR",
           "-o", "ConnectTimeout=5",
           f"{host.get('user')}@{host.get('host')}", "bash -s"]
    try:
        r = subprocess.run(cmd, input=LINUX_PROBE.encode(), capture_output=True, timeout=25)
        out = r.stdout.decode("utf-8", "replace")
        if r.returncode != 0 and not out:
            return {"name": host.get("name") or host.get("host"), "online": False,
                    "error": (r.stderr.decode("utf-8", "replace") or "ssh failed")[:150]}
        return parse_linux(host, out)
    except Exception as exc:  # noqa: BLE001
        return {"name": host.get("name") or host.get("host"), "online": False,
                "error": str(exc)[:150]}


def ssh_linux(host: dict, cmd: str, timeout: int = 30) -> tuple[int, str]:
    args = ["ssh", "-i", host.get("key") or "/keys/opensuse",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=6",
            f"{host.get('user')}@{host.get('host')}", cmd]
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)[:150]


def linux_alive() -> dict | None:
    for item in get("linux") or []:
        if item.get("online"):
            for cfg in LINUX_HOSTS:
                if cfg.get("name") == item.get("name") or cfg.get("host") == item.get("name"):
                    return cfg
            return {"name": item.get("name"), "host": item.get("name")}
    return None


def linux_loop() -> None:
    while True:
        data = [probe_linux(h) for h in LINUX_HOSTS]
        put("linux", data)
        time.sleep(5)


# ------------------------------------------------------------------ actions ---

def wol_send(mac: str | None = None) -> str:
    mac = (mac or load_settings().get("wol_mac") or WIN_MAC).replace("-", "").replace(":", "")
    raw = bytes.fromhex(mac)
    pkt = b"\xff" * 6 + raw * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sent = 0
    for addr in ("255.255.255.255", "192.168.7.255", "192.168.0.255"):
        for port in (9, 7):
            try:
                sock.sendto(pkt, (addr, port))
                sent += 1
            except OSError:
                pass
    sock.close()
    return f"magic packet {mac} ({sent} отправок)"


def wait_until(fn, timeout: float, step: float = 2.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return False


def win_online() -> bool:
    return bool(get("win", {}).get("online"))


def comfy_online() -> bool:
    return http_json(COMFY_LOCAL + "/system_stats", timeout=2) is not None


def act_comfy_on(payload: dict) -> dict:
    if comfy_online():
        return {"detail": "ComfyUI уже работает"}
    ssh_run(f"Stop-ScheduledTask -TaskName '{COMFY_TASK}' -ErrorAction SilentlyContinue; "
            f"Start-Sleep -Seconds 1; Start-ScheduledTask -TaskName '{COMFY_TASK}'", timeout=40)
    ok = wait_until(comfy_online, 120, 3)
    return {"ok": ok, "detail": "ComfyUI поднялся" if ok
            else "задача запущена, но порт 8188 не открылся"}


def act_comfy_off(payload: dict) -> dict:
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ComfyUI*main.py*' } "
              "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    ssh_run(script, timeout=30)
    ok = wait_until(lambda: not comfy_online(), 30, 2)
    return {"ok": ok, "detail": "ComfyUI остановлен" if ok else "процесс не завершился"}


def act_container(payload: dict) -> dict:
    name = str(payload.get("name") or "")
    mode = payload.get("mode") or "restart"
    allowed = {i["name"] for i in (get("docker", {}).get("items") or [])}
    if name not in allowed:
        return {"ok": False, "detail": f"контейнер {name} не найден"}
    r = subprocess.run(["docker", "restart" if mode == "restart" else "start", name],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or r.stdout)[:200]}

    def up():
        return any(i["name"] == name and i["state"] == "running" for i in docker_ps()["items"])
    ok = wait_until(up, 60, 3)
    return {"ok": ok, "detail": f"{name}: {'работает' if ok else 'не поднялся'}"}


def _boot_via_windows(target: str) -> dict:
    guid = OS_ENTRIES[target]
    out = ssh_run(f"bcdedit /set '{{fwbootmgr}}' bootsequence '{guid}'; "
                  f"Start-Sleep -Seconds 2; bcdedit /enum '{{fwbootmgr}}'", timeout=40)
    if "bootsequence" not in out.lower():
        return {"ok": False, "detail": "не удалось задать загрузку: " + out[:150]}
    ssh_run("shutdown /r /t 5 /f", timeout=20)
    return {"ok": True, "detail": f"перезагрузка в {target}"}


def act_boot_os(payload: dict) -> dict:
    target = payload.get("os") if payload.get("os") in OS_ENTRIES else load_settings()["default_os"]
    default = load_settings()["default_os"]

    already = win_online() if target == "windows" else bool(linux_alive())
    if already:
        return {"detail": f"{target} уже загружена"}

    if not (win_online() or linux_alive()):
        wol_send()
        if not wait_until(lambda: win_online() or bool(linux_alive()), 180, 3):
            return {"ok": False, "detail": "WoL отправлен, ПК не ответил (проверьте BIOS про WoL)"}
        if (win_online() if target == "windows" else bool(linux_alive())):
            return {"detail": f"WoL отправлен, грузится {target}"}

    if win_online():
        return _boot_via_windows(target)

    host = linux_alive()
    if not host:
        wol_send()
        wait_until(lambda: win_online() or bool(linux_alive()), 180, 4)
        if win_online():
            return _boot_via_windows(target)
        host = linux_alive()
        if not host:
            return {"ok": False, "detail": "ПК не поднялся: WoL мог не сработать (BIOS)"}

    if target != "windows":
        return {"ok": False, "detail": f"сейчас загружена Linux-система; переключение на {target} не поддерживается"}

    entry = host.get("grub_windows_entry") or "Windows Boot Manager"
    rc, out = ssh_linux(host, f'sudo -n grub2-reboot "{entry}" && echo GRUB-SET')
    if "GRUB-SET" not in out:
        return {"ok": False, "detail": "grub2-reboot не сработал: " + out[:150]}
    ssh_linux(host, "sudo -n systemctl reboot", timeout=15)
    return {"ok": True, "detail": "перезагрузка в Windows через GRUB"}


def _shutdown_windows() -> dict:
    ssh_run("shutdown /s /t 5 /f", timeout=20)
    ok = wait_until(lambda: not win_online(), 180, 3)
    return {"ok": ok, "detail": "Windows выключается" if ok
            else "команда отправлена, но ПК всё ещё отвечает"}


def _boot_windows_from_linux(host: dict) -> dict:
    entry = host.get("grub_windows_entry") or "Windows Boot Manager"
    rc, out = ssh_linux(host, f'sudo -n grub2-reboot "{entry}" && echo GRUB-SET')
    if "GRUB-SET" not in out:
        return {"ok": False, "detail": "grub2-reboot не сработал: " + out[:120]}
    ssh_linux(host, "sudo -n systemctl reboot", timeout=15)
    return {"ok": True}


def act_pc_off(payload: dict) -> dict:
    if win_online():
        return _shutdown_windows()

    host = linux_alive()
    if not host:
        return {"ok": True, "detail": "ПК уже выключен"}

    # Выключение из Linux прошивка не переводит в режим Wake-on-LAN: после
    # такого poweroff магический пакет ПК не поднимает. Поэтому сначала
    # перезагружаемся в Windows и выключаем уже из неё.
    name = host.get("name") or "Linux"
    note(f"перезагружаю {name} в Windows (~1–2 минуты)…")
    res = _boot_windows_from_linux(host)
    if not res.get("ok"):
        note("переключение в Windows не удалось, выключаю напрямую из Linux…")
        ssh_linux(host, "sudo -n systemctl poweroff", timeout=15)
        ok = wait_until(lambda: not bool(linux_alive()), 180, 3)
        return {"ok": False, "detail": (
            "переключение в Windows не удалось, выключил из Linux — "
            "разбудить WoL после такого выключения не получится" if ok
            else "команда отправлена, но ПК всё ещё отвечает")}

    note("жду загрузку Windows…")
    if not wait_until(win_online, 300, 4):
        return {"ok": False, "detail": f"{name} перезагружена в Windows, но Windows "
                                       "не поднялась: ПК остался включённым"}

    note("Windows загрузилась, выключаю…")
    res = _shutdown_windows()
    if res.get("ok"):
        return {"ok": True, "detail": "выключен из Windows — WoL готов"}
    return {"ok": False, "detail": "Windows загрузилась, но выключить не удалось: "
                                   + str(res.get("detail") or "")}


def act_wol(payload: dict) -> dict:
    return {"ok": True, "detail": wol_send()}


def act_settings(payload: dict) -> dict:
    return {"ok": True, "detail": "настройки сохранены", "settings": save_settings(payload)}


ACTIONS = {
    "wol": act_wol,
    "pc_off": act_pc_off,
    "comfy_on": act_comfy_on,
    "comfy_off": act_comfy_off,
    "container": act_container,
    "boot_os": act_boot_os,
    "settings": act_settings,
}


def action_worker() -> None:
    while True:
        job = ACTION_Q.get()
        job_id = job.get("id")
        name = job.get("action")
        payload = job.get("payload") or {}
        put("action", {"id": job_id, "name": name, "state": "running",
                       "detail": "выполняется…", "ts": time.time()})
        CURRENT_JOB["id"], CURRENT_JOB["name"] = job_id, name
        try:
            fn = ACTIONS.get(name)
            if not fn:
                raise ValueError(f"неизвестное действие: {name}")
            res = fn(payload) or {}
            put("action", {"id": job_id, "name": name,
                           "state": "done" if res.get("ok", True) else "error",
                           "detail": res.get("detail") or "готово", "ts": time.time()})
        except Exception as exc:  # noqa: BLE001
            put("action", {"id": job_id, "name": name, "state": "error",
                           "detail": str(exc)[:200], "ts": time.time()})
        finally:
            CURRENT_JOB["id"] = CURRENT_JOB["name"] = None


# --------------------------------------------------------------------- HTTP ---

class Handler(BaseHTTPRequestHandler):
    server_version = "HermesDash/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/all":
            payload = {"ts": time.time()}
            with LOCK:
                payload.update({k: v for k, v in STATE.items() if k != "tunnel_pid"})
                payload["history"] = {k: list(v) for k, v in HIST.items()}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if path == "/manifest.json":
            manifest = {
                "name": "Дом", "short_name": "Дом", "start_url": "./",
                "display": "fullscreen", "orientation": "landscape",
                "background_color": "#070b10", "theme_color": "#070b10",
                "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}],
            }
            self._send(200, json.dumps(manifest, ensure_ascii=False).encode(),
                       "application/manifest+json")
            return
        if path == "/icon.svg":
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
                   '<rect width="192" height="192" rx="36" fill="#070b10"/>'
                   '<circle cx="96" cy="96" r="58" fill="none" stroke="#58c4dc" stroke-width="14" '
                   'stroke-dasharray="270 95" stroke-linecap="round" transform="rotate(-90 96 96)"/>'
                   '<circle cx="96" cy="96" r="30" fill="none" stroke="#4cc38a" stroke-width="12" '
                   'stroke-dasharray="120 70" stroke-linecap="round" transform="rotate(-90 96 96)"/></svg>')
            self._send(200, svg.encode(), "image/svg+xml")
            return
        if path == "/media":
            qs = urllib.parse.parse_qs(parsed.query)
            rel = (qs.get("p") or [""])[0]
            if not rel:
                self._send(400, b"no path", "text/plain")
                return
            try:
                fpath, ctype = fetch_media(rel)
            except Exception as exc:  # noqa: BLE001
                self._send(404, str(exc).encode("utf-8", "replace"), "text/plain; charset=utf-8")
                return
            self._serve_file(fpath, ctype)
            return
        if path in ("/", "/pc", "/gen", "/services", "/misc", "/settings"):
            try:
                with open(os.path.join(ROOT, "index.html"), "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send(500, b"index.html missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/action":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            self._send(400, b'{"ok":false,"detail":"bad json"}', "application/json")
            return
        name = body.get("action")
        if DEMO:
            self._send(200, json.dumps({"ok": False, "detail": "демо-режим: управление отключено"},
                                       ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if name not in ACTIONS:
            self._send(400, json.dumps({"ok": False, "detail": "action not allowed"}).encode(),
                       "application/json; charset=utf-8")
            return
        job = {"id": f"{int(time.time() * 1000)}", "action": name, "payload": body.get("payload") or {}}
        ACTION_Q.put(job)
        self._send(202, json.dumps({"ok": True, "queued": True, "id": job["id"]}).encode(),
                   "application/json; charset=utf-8")

    def _serve_file(self, fpath: str, ctype: str) -> None:
        size = os.path.getsize(fpath)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-", 1)
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                end = min(end, size - 1)
            except ValueError:
                start, end = 0, size - 1
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=300")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(fpath, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


# ---------------------------------------------------------------------- demo ---

def demo_loop() -> None:
    """Правдоподобные данные для публичного демо: ничего реального не читается."""
    import math
    import random

    names = [s[0] for s in LOCAL_SERVICES] or ["Медиа", "Облако", "Игры", "Панель", "Мониторинг"]
    files = [
        {"rel": "video/demo/forest_loop_00012_.mp4", "mb": 8.4, "ext": ".mp4"},
        {"rel": "video/demo/storm_walk_00007_.mp4", "mb": 6.1, "ext": ".mp4"},
        {"rel": "images/hero_poster_00031_.png", "mb": 2.7, "ext": ".png"},
        {"rel": "images/char_sheet_00004_.png", "mb": 3.9, "ext": ".png"},
    ]
    containers = [
        ("media-server", "Up 6 days (healthy)", "jellyfin"),
        ("cloud", "Up 6 days (healthy)", "nextcloud"),
        ("games", "Up 2 days (healthy)", "valheim"),
        ("bots", "Up 6 days", "discord-bot"),
        ("panel", "Up 6 days (healthy)", "panel"),
        ("dashboard", "Up 12 minutes", "dash"),
    ]
    start = time.time()
    while True:
        el = time.time() - start
        cpu = max(3.0, 22 + 13 * math.sin(el / 11.0) + random.uniform(-5, 5))
        gpu = max(0.0, 44 + 34 * math.sin(el / 7.0 + 1.2) + random.uniform(-8, 8))
        mem_used = 5.6 + 1.4 * math.sin(el / 19.0)
        ram_used = 14.2 + 3.1 * math.sin(el / 13.0)
        hist_push("host_cpu", cpu)
        hist_push("pc_cpu", cpu * 0.7)
        hist_push("pc_gpu", gpu)
        hist_push("host_ram", mem_used / 15.8 * 100)
        hist_push("pc_ram", ram_used / 31.9 * 100)
        hist_push("pc_vram", 38 + 20 * math.sin(el / 9.0))

        put("meta", {"name": SERVER_NAME, "subtitle": SERVER_SUBTITLE, "demo": True})
        put("host", {
            "cpu": {"cpu": round(cpu, 1), "cores": [round(max(1, cpu + random.uniform(-9, 9)), 1) for _ in range(8)]},
            "mem": {"total": 16.95e9, "used": mem_used * 1e9, "avail": (16.95 - mem_used) * 1e9,
                    "swap_total": 1.03e9, "swap_used": 0.02e9},
            "load": [round(cpu / 100 * 1.6, 2), round(cpu / 100 * 1.3, 2), 0.52],
            "uptime": int(6 * 86400 + el),
            "disks": [
                {"mount": "/data", "dev": "VG1-data", "fs": "ext4", "total": 1.97e12,
                 "used": 1.42e12, "free": 0.55e12},
                {"mount": "/", "dev": "HACTRII--vg-root", "fs": "ext4", "total": 57e9,
                 "used": 24e9, "free": 33e9},
            ],
            "net": {"rx": 4.2e6 + random.uniform(0, 2e6), "tx": 1.1e6 + random.uniform(0, 6e5),
                    "total_rx": 1.44e9, "total_tx": 3.1e8},
            "temps": [{"name": "Package id 0", "c": round(44 + random.uniform(-2, 6), 1)},
                      {"name": "Core 0", "c": round(43 + random.uniform(-2, 5), 1)},
                      {"name": "NVMe", "c": round(38 + random.uniform(-1, 3), 1)}],
        })
        put("win", {
            "online": True,
            "cpu": int(18 + 9 * math.sin(el / 8.0) + random.uniform(-4, 4)),
            "ram_total": 34.2e9, "ram_free": 34.2e9 - ram_used * 1e9,
            "uptime": int(2 * 86400 + el),
            "gpus": [{"name": "NVIDIA GeForce RTX 3060", "util": round(gpu, 1),
                      "vram_used": 4.8 + 2.2 * math.sin(el / 9.0), "vram_total": 12288.0,
                      "temp": round(52 + random.uniform(-3, 8), 1),
                      "power": round(70 + random.uniform(-10, 40), 1), "fan": 42.0}],
            "disks": [{"mount": "C:", "total": 915e9, "free": 145e9}],
            "procs_ram": [{"name": "python", "mb": 1580}, {"name": "Telegram", "mb": 512},
                          {"name": "firefox", "mb": 421}, {"name": "Discord", "mb": 405}],
            "procs_cpu": [{"name": "python", "cpu": 640}, {"name": "System", "cpu": 530}],
            "comfy_local": True, "ollama_local": True,
        })
        put("comfy", {"online": True, "vram_total": 12884377600,
                      "vram_free": 12884377600 - int(gpu / 100 * 8.6e9),
                      "device": "cuda:0 NVIDIA GeForce RTX 3060",
                      "comfy_version": "0.3.40", "py": "3.12.8",
                      "running": 1 if int(el) % 40 < 18 else 0,
                      "pending": int(el) % 3, "running_id": "a1b2c3d4"})
        put("ollama", {"online": True, "models": [
            {"name": "qwen3.5:9b", "size": 5.8e9},
            {"name": "gpt-oss-20b", "size": 12.1e9}]})
        put("docker", {"ok": True,
                       "items": [{"name": n, "state": "running", "status": st, "image": img}
                                 for n, st, img in containers],
                       "running": len(containers), "total": len(containers)})
        put("services", [{"name": n, "url": "https://example.local/" + str(i), "code": 200,
                          "ok": True, "state": "ok", "ms": random.randint(9, 44)}
                         for i, n in enumerate(names)])
        put("files", files)
        put("weather", {"ok": True, "city": WEATHER_CITY or "демо-город", "temp": -3.4,
                        "feels": -8.1, "desc": "Небольшой снег", "kind": "snow",
                        "wind": 5, "hum": 78,
                        "days": [{"date": "2026-11-15", "min": -7, "max": -2, "desc": "Снег", "kind": "snow"},
                                 {"date": "2026-11-16", "min": -9, "max": -4, "desc": "Пасмурно", "kind": "cloud"},
                                 {"date": "2026-11-17", "min": -6, "max": -1, "desc": "Ясно", "kind": "sun"}]})
        time.sleep(3)


def main() -> None:
    put("settings", load_settings())
    put("meta", {"name": SERVER_NAME, "subtitle": SERVER_SUBTITLE, "demo": DEMO})

    if DEMO:
        threading.Thread(target=demo_loop, daemon=True).start()
    else:
        threading.Thread(target=action_worker, daemon=True).start()
        loops = [host_loop, services_loop]
        if WEATHER_CFG.get("lat"):
            loops.append(weather_loop)
        if WINDOWS_ENABLED:
            loops += [tunnel_loop, win_loop, win_files_loop, comfy_loop, ollama_loop]
        if LINUX_HOSTS:
            loops.append(linux_loop)
        for loop in loops:
            threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"dashboard v2 on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
