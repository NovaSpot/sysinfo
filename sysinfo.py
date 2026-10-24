#!/usr/bin/env python3
"""sysinfo: a tile-based system information dashboard for the Linux terminal.

    sysinfo.py                  live dashboard (refreshes every 2s), Ctrl+C to exit
    sysinfo.py --once           print one snapshot and exit
    sysinfo.py -i 1 --theme tokyo
    sysinfo.py --public-ip      also look up your public IP (off by default)

Dependencies (Debian):  sudo apt install python3-rich python3-psutil
"""
from __future__ import annotations

import argparse
import getpass
import glob
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime

try:
    import psutil
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.exit(
        "Missing dependencies. On Debian run:\n"
        "  sudo apt install python3-rich python3-psutil\n"
        "or use a virtualenv:\n"
        "  python3 -m venv .venv && . .venv/bin/activate && pip install rich psutil"
    )

TOOL_NAME = "sysinfo"
LABEL_W = 9
LOADING = object()

# --------------------------------------------------------------------------- #
# Themes. Every colour is assigned by role, so a theme is just a dict.
# --------------------------------------------------------------------------- #
THEMES = {
    "mocha": dict(bright="#cdd6f4", muted="#a6adc8", faint="#6c7086", border="#45475a",
                  empty="#45475a", title="#b4befe", ok="#a6e3a1", warn="#f9e2af",
                  crit="#f38ba8", blue="#89b4fa", peach="#fab387", mauve="#cba6f7",
                  teal="#94e2d5", bar="#181825"),
    "tokyo": dict(bright="#c0caf5", muted="#9aa5ce", faint="#565f89", border="#414868",
                  empty="#414868", title="#bb9af7", ok="#9ece6a", warn="#e0af68",
                  crit="#f7768e", blue="#7aa2f7", peach="#ff9e64", mauve="#bb9af7",
                  teal="#73daca", bar="#16161e"),
    "nord": dict(bright="#eceff4", muted="#d8dee9", faint="#616e88", border="#4c566a",
                 empty="#4c566a", title="#88c0d0", ok="#a3be8c", warn="#ebcb8b",
                 crit="#bf616a", blue="#81a1c1", peach="#d08770", mauve="#b48ead",
                 teal="#8fbcbb", bar="#2e3440"),
}
T: dict = dict(THEMES["mocha"])


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def read(path, default=None):
    try:
        with open(path, "r", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return default


def run(cmd, timeout=1.5):
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def run_rc(cmd, timeout=1.5):
    """Like run(), but returns (exit_code, stdout) so callers can accept non-zero codes."""
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
    except (subprocess.SubprocessError, OSError):
        return None
    return r.returncode, r.stdout


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def human_size(n):
    """Compact size for tight columns: 58G, 256M, 4.4G."""
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            if unit == "B":
                return f"{n:.0f}B"
            return f"{n:.0f}{unit}" if n >= 10 else f"{n:.1f}{unit}"
        n /= 1024


def human_gib(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_rate(n):
    n = float(n)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if n < 1024 or unit == "GB/s":
            return f"{n:.0f} {unit}" if (n >= 100 or unit == "B/s") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def sev(value, warn=70, crit=90):
    return 2 if value >= crit else 1 if value >= warn else 0


def scolor(value, warn=70, crit=90):
    return T[("ok", "warn", "crit")[sev(value, warn, crit)]]


VENDORS = [
    (r"ASUS", "ASUS"), (r"\bDell\b", "Dell"), (r"Hewlett|\bHP\b", "HP"),
    (r"LENOVO", "Lenovo"), (r"\bAcer\b", "Acer"), (r"Micro-Star|\bMSI\b", "MSI"),
    (r"GIGABYTE", "Gigabyte"), (r"Apple", "Apple"), (r"Intel", "Intel"),
    (r"Realtek", "Realtek"), (r"Advanced Micro|\bAMD\b|\bATI\b", "AMD"),
    (r"NVIDIA", "NVIDIA"), (r"Qualcomm|Atheros", "Qualcomm"), (r"MediaTek", "MediaTek"),
    (r"Broadcom", "Broadcom"), (r"Samsung", "Samsung"), (r"Western Digital|\bWD\b", "WD"),
    (r"Kingston", "Kingston"), (r"Micron|Crucial", "Micron"), (r"SK ?hynix|Hynix", "SK hynix"),
    (r"QEMU", "QEMU"), (r"VMware", "VMware"), (r"innotek|VirtualBox|Oracle", "VirtualBox"),
    (r"Microsoft", "Microsoft"), (r"Huawei", "Huawei"), (r"Xiaomi", "Xiaomi"),
    (r"Framework", "Framework"),
]


def short_vendor(v):
    if not v:
        return None
    for pat, name in VENDORS:
        if re.search(pat, v, re.I):
            return name
    v = re.sub(r"\b(Corporation|Corp\.?|Inc\.?|Co\.?,?|Ltd\.?|Limited|Technologies|Technology|"
               r"Semiconductor|Computer|Electronics|GmbH)\b", "", v, flags=re.I)
    return re.sub(r"\s+", " ", v).strip(" ,.") or None


def short_cpu(n):
    n = re.sub(r"\((R|TM|r|tm)\)", "", n)
    n = re.sub(r"\b\d+(th|st|nd|rd) Gen\b", "", n)
    n = re.sub(r"\b(with|w/) Radeon.*$", "", n)
    n = re.sub(r"@.*$", "", n)
    n = re.sub(r"\b(CPU|Processor|Intel|AMD)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


# --------------------------------------------------------------------------- #
# Text building blocks
# --------------------------------------------------------------------------- #
def txt(s, color="bright", bold=False):
    return Text(str(s), style=("bold " if bold else "") + T.get(color, color))


def lab(label):
    return Text(f"{label:<{LABEL_W}}", style=T["muted"])


def kv(label, value, w, color="bright"):
    t = lab(label)
    if isinstance(value, Text):
        t.append_text(value)
    else:
        t.append(str(value), style=T.get(color, color))
    t.truncate(w, overflow="ellipsis")
    return t


def split(left, right, w):
    left = left if isinstance(left, Text) else txt(left)
    right = right if isinstance(right, Text) else txt(right, "muted")
    space = w - left.cell_len - right.cell_len
    if space < 1:
        left.truncate(max(w - right.cell_len - 1, 1), overflow="ellipsis")
        space = max(w - left.cell_len - right.cell_len, 1)
    out = Text()
    out.append_text(left)
    out.append(" " * space)
    out.append_text(right)
    return out


def bar(pct, width, color=None):
    width = max(width, 1)
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = round(width * pct / 100)
    if pct > 0 and filled == 0:
        filled = 1
    t = Text()
    t.append("█" * filled, style=color or scolor(pct))
    t.append("░" * (width - filled), style=T["empty"])
    return t


def bar_row(label, pct, right, w, color=None):
    right = right if isinstance(right, Text) else txt(right)
    bw = w - LABEL_W - right.cell_len - 1
    t = lab(label)
    if bw >= 4:
        t.append_text(bar(pct, bw, color))
        t.append(" ")
    t.append_text(right)
    return t


SPARK = "▁▂▃▄▅▆▇█"


def spark(values, width, vmax=None, color="blue"):
    vals = list(values)[-width:]
    top = vmax if vmax else max(vals + [1e-9])
    t = Text(" " * (width - len(vals)))
    cells = ""
    for v in vals:
        cells += SPARK[int(min(max(v / top, 0), 1) * 7)]
    t.append(cells, style=T.get(color, color))
    return t


def badge(label, color):
    return Text(f" {label} ", style=f"bold {T['bar']} on {T[color]}")


# --------------------------------------------------------------------------- #
# Data sources (everything is read-only and fails soft)
# --------------------------------------------------------------------------- #
def cpu_model():
    for line in (read("/proc/cpuinfo", "") or "").splitlines():
        if line.lower().startswith("model name"):
            return short_cpu(line.split(":", 1)[1].strip())
    return read("/proc/device-tree/model") or platform.processor() or platform.machine()


def cpu_temp():
    fn = getattr(psutil, "sensors_temperatures", None)
    temps = fn() if fn else {}
    for name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz"):
        entries = temps.get(name)
        if entries:
            for e in entries:
                if e.label in ("Package id 0", "Tctl", "Tdie"):
                    return e.current
            return entries[0].current
    return None


def secure_boot():
    for p in glob.glob("/sys/firmware/efi/efivars/SecureBoot-*"):
        try:
            with open(p, "rb") as f:
                return f.read()[-1] == 1
        except OSError:
            return None
    return None


_LSPCI: dict = {}


def lspci_names(slot):
    if slot in _LSPCI:
        return _LSPCI[slot]
    res = (None, None)
    out = run(["lspci", "-s", slot, "-mm"])
    if out:
        try:
            f = shlex.split(out.splitlines()[0])
            m = re.search(r"\[(.+)\]", f[3])
            res = (short_vendor(f[2]), m.group(1) if m else f[3])
        except (ValueError, IndexError):
            pass
    _LSPCI[slot] = res
    return res


def gpu_info():
    if shutil.which("nvidia-smi"):
        out = run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,"
                   "memory.total,clocks.gr", "--format=csv,noheader,nounits"], timeout=2)
        if out:
            p = [x.strip() for x in out.splitlines()[0].split(",")]
            if len(p) >= 6:
                return dict(name=re.sub(r"^NVIDIA ", "", p[0]), kind="dedicated",
                            usage=fnum(p[1]), temp=fnum(p[2]),
                            vram=((fnum(p[3]) or 0) * 2**20, (fnum(p[4]) or 0) * 2**20),
                            clock=fnum(p[5]), driver="nvidia")
    found = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if "-" in os.path.basename(card):
            continue
        dev = card + "/device"
        if not (read(dev + "/class") or "").startswith("0x03"):
            continue
        slot = os.path.basename(os.path.realpath(dev))
        vname, dname = lspci_names(slot)
        info = dict(name=dname or (f"{vname} Graphics" if vname else "GPU"), kind="integrated",
                    usage=fnum(read(dev + "/gpu_busy_percent")), temp=None, vram=None,
                    clock=None, driver=None)
        if os.path.exists(dev + "/driver"):
            info["driver"] = os.path.basename(os.path.realpath(dev + "/driver"))
        vt, vu = fnum(read(dev + "/mem_info_vram_total")), fnum(read(dev + "/mem_info_vram_used"))
        if vt and vu is not None:
            info["vram"] = (vu, vt)
            info["kind"] = "dedicated" if vt >= 2**30 else "integrated"
        if read(dev + "/vendor") == "0x10de":
            info["kind"] = "dedicated"
        for t in glob.glob(dev + "/hwmon/hwmon*/temp1_input"):
            v = fnum(read(t))
            if v is not None:
                info["temp"] = v / 1000
        info["clock"] = fnum(read(card + "/gt_cur_freq_mhz"))
        if info["clock"] is None:
            for line in (read(dev + "/pp_dpm_sclk", "") or "").splitlines():
                m = re.search(r"(\d+)\s*Mhz\s*\*", line, re.I)
                if m:
                    info["clock"] = float(m.group(1))
        found.append(info)
    if not found:
        return None
    return max(found, key=lambda i: (i["vram"][1] if i["vram"] else 0))


def default_iface():
    for line in (read("/proc/net/route", "") or "").splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[1] == "00000000" and int(f[3], 16) & 2:
            return f[0]
    try:
        for name, st in psutil.net_if_stats().items():
            if st.isup and name != "lo":
                return name
    except Exception:
        pass
    return None


def wifi_signal(iface):
    for line in (read("/proc/net/wireless", "") or "").splitlines()[2:]:
        if line.strip().startswith(iface + ":"):
            level = fnum(line.split()[3].rstrip("."))
            if level is None:
                return None
            if level > 0:
                level -= 256
            return 4 if level >= -55 else 3 if level >= -65 else 2 if level >= -75 else 1
    return None


def wifi_ssid():
    out = run(["iwgetid", "-r"])
    if out and out.strip():
        return out.strip()
    out = run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], timeout=2)
    for line in (out or "").splitlines():
        if line.startswith("yes:"):
            return line[4:]
    return None


def nic_vendor(iface):
    if not iface:
        return None
    dev = f"/sys/class/net/{iface}/device"
    if not os.path.exists(dev):
        return None
    real = os.path.realpath(dev)
    vendor, _ = lspci_names(os.path.basename(real))
    if vendor:
        return vendor
    return short_vendor(read(real + "/../manufacturer"))


def fetch_public_ip():
    with urllib.request.urlopen("https://api.ipify.org", timeout=3) as r:
        return r.read().decode().strip()


def root_drive():
    dev = fstype = None
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint == "/":
            dev, fstype = p.device, p.fstype
    if not dev:
        return None
    name = os.path.basename(os.path.realpath(dev))
    for _ in range(4):
        parent = (run(["lsblk", "-no", "pkname", "/dev/" + name]) or "").split()
        if not parent:
            break
        name = parent[0]
    base = f"/sys/block/{name}"
    model = read(base + "/device/model")
    sectors = fnum(read(base + "/size"))
    if name.startswith("nvme"):
        kind = "NVMe SSD"
    else:
        kind = "HDD" if read(base + "/queue/rotational") == "1" else "SSD"
    return dict(model=re.sub(r"\s+", " ", model) if model else None,
                size=human_size(sectors * 512).replace("B", "") + "B" if sectors else None,
                kind=kind if os.path.exists(base) else None, fstype=fstype,
                dev=name if os.path.exists(base) else None)


def list_mounts():
    seen = {}
    for p in psutil.disk_partitions(all=False):
        if (p.fstype in ("squashfs", "iso9660") or p.device.startswith("/dev/loop")
                or p.mountpoint.startswith(("/snap", "/var/snap", "/var/lib/docker", "/run"))):
            continue
        if p.device in seen and len(p.mountpoint) >= len(seen[p.device].mountpoint):
            continue
        seen[p.device] = p
    return sorted(seen.values(), key=lambda p: (p.mountpoint != "/", p.mountpoint))


def count_packages():
    if os.path.exists("/var/lib/dpkg/status"):
        n = 0
        with open("/var/lib/dpkg/status", errors="ignore") as f:
            for line in f:
                if line == "Status: install ok installed\n":
                    n += 1
        return f"{n} (dpkg)"
    if os.path.isdir("/var/lib/pacman/local"):
        return f"{max(len(os.listdir('/var/lib/pacman/local')) - 1, 0)} (pacman)"
    out = run(["rpm", "-qa"], timeout=5)
    return f"{len(out.splitlines())} (rpm)" if out else None


def shell_name():
    sh = os.environ.get("SHELL")
    if not sh:
        return None
    name = os.path.basename(sh)
    out = run([sh, "--version"]) or ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out.splitlines()[0] if out else "")
    return f"{name} {m.group(1)}" if m else name


def usb_devices():
    names = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        if ":" in os.path.basename(d):
            continue
        prod = read(d + "/product")
        if not prod or read(d + "/bDeviceClass") == "09":
            continue
        if re.search(r"hub|host controller|bluetooth", prod, re.I):
            continue
        if prod not in names:
            names.append(prod)
    return names


def bluetooth_devices():
    out = run(["bluetoothctl", "devices"], timeout=2)
    res = []
    for line in (out or "").splitlines()[:10]:
        m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.+)", line)
        if not m:
            continue
        info = run(["bluetoothctl", "info", m.group(1)], timeout=2) or ""
        if "Connected: yes" not in info:
            continue
        b = re.search(r"Battery Percentage: 0x[0-9a-fA-F]+ \((\d+)\)", info)
        res.append((m.group(2), int(b.group(1)) if b else None))
    return res


def audio_info():
    if shutil.which("pactl"):
        sink = (run(["pactl", "get-default-sink"]) or "").strip()
        source = (run(["pactl", "get-default-source"]) or "").strip()
        if sink:
            vol = re.search(r"(\d+)%", run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]) or "")
            muted = "yes" in (run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]) or "")
            desc = {}
            for kind in ("sinks", "sources"):
                cur = None
                for line in (run(["pactl", "list", kind]) or "").splitlines():
                    s = line.strip()
                    if s.startswith("Name:"):
                        cur = s[5:].strip()
                    elif s.startswith("Description:") and cur:
                        desc[cur] = s[12:].strip()
            return dict(out=desc.get(sink, sink), vol=int(vol.group(1)) if vol else None,
                        muted=muted, inp=desc.get(source, source) or None)
    out = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    if out:
        m = re.search(r"([\d.]+)", out)

        def node(which):
            info = run(["wpctl", "inspect", which]) or ""
            d = re.search(r'node\.description = "(.*)"', info)
            return d.group(1) if d else None

        return dict(out=node("@DEFAULT_AUDIO_SINK@") or "default output",
                    vol=round(float(m.group(1)) * 100) if m else None,
                    muted="MUTED" in out, inp=node("@DEFAULT_AUDIO_SOURCE@"))
    return None


PNP = {"BOE": "BOE", "AUO": "AUO", "LGD": "LG Display", "SDC": "Samsung Display",
       "SAM": "Samsung", "DEL": "Dell", "GSM": "LG", "ACR": "Acer", "AOC": "AOC",
       "AUS": "ASUS", "BNQ": "BenQ", "HWP": "HP", "LEN": "Lenovo", "CMN": "Innolux",
       "IVO": "InfoVision", "CSO": "CSOT", "MSI": "MSI", "PHL": "Philips",
       "VSC": "ViewSonic", "APP": "Apple", "SHP": "Sharp", "NCP": "Najing CEC"}


def parse_edid(b):
    if len(b) < 128 or b[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None, None
    m = (b[8] << 8) | b[9]
    code = "".join(chr(((m >> s) & 0x1F) + 64) for s in (10, 5, 0))
    name = None
    for off in (54, 72, 90, 108):
        blk = b[off:off + 18]
        if blk[:3] == b"\x00\x00\x00" and blk[3] == 0xFC:
            name = blk[5:18].split(b"\n")[0].decode("ascii", "ignore").strip()
    return PNP.get(code, code), name


def display_info():
    rates = {}
    if os.environ.get("DISPLAY"):
        cur = None
        for line in (run(["xrandr", "--current"]) or "").splitlines():
            m = re.match(r"(\S+) connected", line)
            if m:
                cur = m.group(1)
            elif cur and "*" in line:
                for tok in line.split()[1:]:
                    if "*" in tok:
                        rates[cur] = fnum(tok.strip("*+"))
                        break
    mons = []
    for st in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        if read(st) != "connected":
            continue
        conn = os.path.dirname(st)
        name = re.sub(r"^card\d+-", "", os.path.basename(conn))
        modes = (read(conn + "/modes") or "").splitlines()
        vendor = model = None
        try:
            with open(conn + "/edid", "rb") as f:
                vendor, model = parse_edid(f.read())
        except OSError:
            pass
        rate = rates.get(name)
        if rate is None and len(rates) == 1:
            rate = next(iter(rates.values()))
        mons.append(dict(name=name, mode=modes[0] if modes else None, rate=rate,
                         vendor=vendor, model=model))
    return mons


def battery_info():
    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bats:
        return None
    b = bats[0]
    cap = fnum(read(b + "/capacity"))
    if cap is None:
        return None
    status = read(b + "/status", "Unknown")
    volt = fnum(read(b + "/voltage_now"))
    power = fnum(read(b + "/power_now"))
    if power is not None:
        watts = power / 1e6
    else:
        cur = fnum(read(b + "/current_now"))
        watts = cur * volt / 1e12 if cur is not None and volt is not None else None
    left = None
    try:
        sb = psutil.sensors_battery()
        if sb and sb.secsleft and sb.secsleft > 0:
            left = sb.secsleft
    except Exception:
        pass
    if left is None and watts:
        en, ef = fnum(read(b + "/energy_now")), fnum(read(b + "/energy_full"))
        if en is not None and ef is not None:
            left = (en if status == "Discharging" else ef - en) / (watts * 1e6) * 3600
    full, design = fnum(read(b + "/energy_full")), fnum(read(b + "/energy_full_design"))
    if full is None or design is None:
        full, design = fnum(read(b + "/charge_full")), fnum(read(b + "/charge_full_design"))
    return dict(pct=cap, status=status, watts=watts, left=left,
                health=full / design * 100 if full and design else None,
                cycles=fnum(read(b + "/cycle_count")))


def read_pressure(kind="memory"):
    """Pressure-stall info from /proc/pressure/<kind> (avg10, in %): {'some': x, 'full': y}.
    None when the kernel has PSI disabled or doesn't provide it."""
    text = read(f"/proc/pressure/{kind}")
    if not text:
        return None
    res = {}
    for line in text.splitlines():
        parts = line.split()
        for p in parts[1:]:
            if p.startswith("avg10="):
                v = fnum(p[6:])
                if v is not None:
                    res[parts[0]] = v
    return res or None


def disk_io_totals():
    """(read_bytes, write_bytes) summed over physical disks only, so partitions,
    loop devices and dm/LVM layers aren't counted twice. None if unavailable."""
    try:
        per = psutil.disk_io_counters(perdisk=True)
    except Exception:
        return None
    r = w = 0
    found = False
    for name, st in (per or {}).items():
        if os.path.exists(f"/sys/block/{name}/device"):
            r += st.read_bytes
            w += st.write_bytes
            found = True
    return (r, w) if found else None


def fan_speeds():
    fn = getattr(psutil, "sensors_fans", None)
    try:
        data = fn() if fn else {}
    except Exception:
        return []
    return [e.current for entries in (data or {}).values() for e in entries]


def drive_temp(dev):
    """Temperature of a block device (NVMe, or SATA with the drivetemp module), in °C."""
    if not dev:
        return None
    for pat in (f"/sys/block/{dev}/device/hwmon*/temp1_input",
                f"/sys/block/{dev}/device/hwmon/hwmon*/temp1_input"):
        for t in glob.glob(pat):
            v = fnum(read(t))
            if v is not None:
                return v / 1000
    return None


def sensor_data(dev):
    return dict(fans=fan_speeds(), drive_temp=drive_temp(dev))


def pending_updates():
    """Number of upgradable packages according to the local package lists (no network,
    no root). Returns None if the package manager isn't supported or the check fails."""
    if shutil.which("apt-get"):
        res = run_rc(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=30)
        if res and res[0] == 0:
            return sum(1 for ln in res[1].splitlines() if ln.startswith("Inst "))
        return None
    if shutil.which("pacman"):
        res = run_rc(["pacman", "-Qu"], timeout=15)
        if res is None:
            return None
        rc, out = res
        if rc == 0:
            return len([ln for ln in out.splitlines() if ln.strip()])
        return 0 if rc == 1 and not out.strip() else None
    if shutil.which("dnf"):
        res = run_rc(["dnf", "check-update", "--cacheonly", "-q"], timeout=30)
        if res is None:
            return None
        rc, out = res
        if rc == 0:
            return 0
        if rc == 100:
            n = 0
            for ln in out.splitlines():
                if ln.startswith("Obsoleting"):
                    break
                if re.match(r"^\S+\.\S+\s+\S+\s+\S+\s*$", ln):
                    n += 1
            return n
    return None


UPDATES_SUPPORTED = any(shutil.which(x) for x in ("apt-get", "pacman", "dnf"))


def failed_units():
    """Names of failed systemd units (system + user). None if systemd isn't usable here."""
    names, got = [], False
    for cmd in (["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
                ["systemctl", "--user", "--failed", "--no-legend", "--plain", "--no-pager"]):
        out = run(cmd, timeout=3)
        if out is None:
            continue
        got = True
        for ln in out.splitlines():
            f = ln.split()
            if f:
                names.append(f[0])
    return names if got else None


def reboot_required():
    if os.path.exists("/run/reboot-required") or os.path.exists("/var/run/reboot-required"):
        return True
    rel = platform.release()
    if "microsoft" in rel.lower() or os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return False
    # If the running kernel's modules are gone, the kernel was replaced since boot (Arch, Fedora...).
    for base in ("/usr/lib/modules", "/lib/modules"):
        if os.path.isdir(base):
            try:
                kernels = os.listdir(base)
            except OSError:
                return False
            if kernels:
                return rel not in kernels
    return False


# --------------------------------------------------------------------------- #
# Collector: keeps history and refreshes slow data in background threads
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self, public_ip=False):
        self.cpu = 0.0
        self.cpu_hist = deque(maxlen=120)
        self.down_hist = deque(maxlen=120)
        self.up_hist = deque(maxlen=120)
        self.down = self.up = 0.0
        self.iface = None
        self.net_total = None
        self._net_last = None
        self.disk_r_hist = deque(maxlen=120)
        self.disk_w_hist = deque(maxlen=120)
        self.disk_r = self.disk_w = 0.0
        self.has_disk_io = False
        self._disk_last = None
        self.swap_in = self.swap_out = 0.0
        self._swap_last = None
        self.top = []
        self.ntasks = 0
        self.cpu_name = cpu_model()
        self.ncpu = psutil.cpu_count() or 1
        self._slow = {}
        self._slow_t = {}
        self.public_ip = public_ip
        self.slow_jobs = {
            "gpu": (gpu_info, 2), "audio": (audio_info, 3), "bt": (bluetooth_devices, 10),
            "usb": (usb_devices, 5), "display": (display_info, 15), "ssid": (wifi_ssid, 10),
            "drive": (root_drive, 120), "pkgs": (count_packages, 3600),
            "shell": (shell_name, 3600), "nic": (lambda: nic_vendor(self.iface), 60),
            "sensors": (self._sensors, 2), "reboot": (reboot_required, 30),
        }
        if shutil.which("systemctl"):
            self.slow_jobs["services"] = (failed_units, 30)
        if UPDATES_SUPPORTED:
            self.slow_jobs["updates"] = (pending_updates, 1800)
        if public_ip:
            self.slow_jobs["pubip"] = (fetch_public_ip, 300)
        psutil.cpu_percent(None)

    def get(self, key):
        return self._slow.get(key, LOADING)

    def refresh_slow(self, force=False):
        now = time.monotonic()
        for key, (fn, ttl) in self.slow_jobs.items():
            if force or now - self._slow_t.get(key, -1e9) >= ttl:
                self._slow_t[key] = now
                try:
                    self._slow[key] = fn()
                except Exception:
                    self._slow.setdefault(key, None)

    def _sensors(self):
        d = self._slow.get("drive")
        return sensor_data(d.get("dev") if isinstance(d, dict) else None)

    def start_background(self):
        # One thread per job, so a slow one (apt, bluetoothctl...) never delays the others.
        def loop(key, fn, ttl):
            while True:
                try:
                    self._slow[key] = fn()
                except Exception:
                    self._slow.setdefault(key, None)
                time.sleep(ttl)
        for key, (fn, ttl) in self.slow_jobs.items():
            threading.Thread(target=loop, args=(key, fn, ttl), daemon=True).start()

    def sample(self):
        self.cpu = psutil.cpu_percent(None)
        self.cpu_hist.append(self.cpu)
        now = time.monotonic()
        iface = default_iface()
        counters = None
        try:
            counters = psutil.net_io_counters(pernic=True).get(iface) if iface else None
        except Exception:
            pass
        if iface != self.iface:
            self._net_last = None
            self.down_hist.clear()
            self.up_hist.clear()
            self.down = self.up = 0.0
        self.iface = iface
        if counters:
            if self._net_last:
                _, t0, rx0, tx0 = self._net_last
                dt = max(now - t0, 1e-3)
                self.down = max(counters.bytes_recv - rx0, 0) / dt
                self.up = max(counters.bytes_sent - tx0, 0) / dt
                self.down_hist.append(self.down)
                self.up_hist.append(self.up)
            self._net_last = (iface, now, counters.bytes_recv, counters.bytes_sent)
            self.net_total = (counters.bytes_recv, counters.bytes_sent)
        else:
            self.net_total = None
        io = disk_io_totals()
        self.has_disk_io = io is not None
        if io:
            if self._disk_last:
                t0, r0, w0 = self._disk_last
                dt = max(now - t0, 1e-3)
                self.disk_r = max(io[0] - r0, 0) / dt
                self.disk_w = max(io[1] - w0, 0) / dt
                self.disk_r_hist.append(self.disk_r)
                self.disk_w_hist.append(self.disk_w)
            self._disk_last = (now, io[0], io[1])
        else:
            self._disk_last = None
            self.disk_r = self.disk_w = 0.0
        try:
            sm = psutil.swap_memory()
            if self._swap_last:
                t0, i0, o0 = self._swap_last
                dt = max(now - t0, 1e-3)
                self.swap_in = max(sm.sin - i0, 0) / dt
                self.swap_out = max(sm.sout - o0, 0) / dt
            self._swap_last = (now, sm.sin, sm.sout)
        except Exception:
            self._swap_last = None
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            i = p.info
            mi = i.get("memory_info")
            procs.append(((i.get("cpu_percent") or 0.0) / self.ncpu, i["pid"],
                          i.get("name") or "?", mi.rss if mi else 0))
        self.ntasks = len(procs)
        procs.sort(key=lambda x: (x[0], x[3]), reverse=True)
        self.top = procs[:8]


# --------------------------------------------------------------------------- #
# Tiles. Each returns (lines, level) where level 0/1/2 = ok/warn/crit border,
# or None to hide the tile entirely (e.g. no battery on a desktop).
# --------------------------------------------------------------------------- #
def loading():
    return [txt("loading…", "faint")], 0


def tile_machine(c, w):
    d = "/sys/class/dmi/id/"
    product = read(d + "product_name")
    if product in (None, "", "System Product Name", "To be filled by O.E.M.", "Default string"):
        product = read(d + "board_name")
    if product and "_" in product:
        a, b = product.split("_", 1)
        if a == b:
            product = a
    vendor_raw = read(d + "sys_vendor") or read(d + "board_vendor")
    bios, date = read(d + "bios_version"), read(d + "bios_date")
    if date and re.match(r"\d\d/\d\d/\d{4}$", date):
        mm, dd, yy = date.split("/")
        date = f"{yy}-{mm}-{dd}"
    uefi = os.path.isdir("/sys/firmware/efi")
    sb = secure_boot() if uefi else None
    lines = [txt(product or platform.node(), bold=True),
             kv("Vendor", short_vendor(vendor_raw) or "–", w)]
    bl = Text(bios or "–", style=T["bright"])
    if date:
        bl.append("  " + date, style=T["faint"])
    lines.append(kv("BIOS", bl, w))
    lines.append(kv("Firmware", "UEFI" if uefi else "BIOS", w))
    if not uefi:
        lines.append(kv("Sec.boot", "n/a", w, "faint"))
    elif sb is None:
        lines.append(kv("Sec.boot", "–", w, "faint"))
    else:
        lines.append(kv("Sec.boot", "ON" if sb else "OFF", w, "ok" if sb else "warn"))
    if re.search(r"VirtualBox|VMware|QEMU|KVM|Bochs|Xen|Virtual Machine", f"{vendor_raw} {product}", re.I):
        lines.append(kv("Virt", "VM", w, "blue"))
    return lines, 0


def fan_text(fans):
    shown = fans[:2]
    t = txt(" / ".join(f"{f:.0f}" for f in shown), "faint" if all(f <= 0 for f in shown) else "bright")
    t.append(" RPM", style=T["muted"])
    return t


def tile_cpu(c, w):
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = f.current if f else None
    except Exception:
        pass
    right = txt("–", "muted") if freq is None else txt(
        f"{freq / 1000:.2f} GHz" if freq >= 1000 else f"{freq:.0f} MHz", "muted")
    lines = [split(txt(c.cpu_name, bold=True), right, w),
             bar_row("Usage", c.cpu, txt(f"{c.cpu:>3.0f}%"), w)]
    level = sev(c.cpu)
    temp = cpu_temp()
    if temp is not None:
        col = scolor(temp, 70, 85)
        lines.append(bar_row("Temp", temp, txt(f"{temp:.0f}°C", col), w, color=col))
        level = max(level, sev(temp, 70, 85))
    else:
        lines.append(kv("Temp", "–", w, "faint"))
    phys, logical = psutil.cpu_count(logical=False), psutil.cpu_count()
    cores = txt(f"{phys}C / {logical}T" if phys else f"{logical}T")
    cores.append("  " + platform.machine(), style=T["faint"])
    lines.append(kv("Cores", cores, w))
    la = os.getloadavg()
    lines.append(kv("Load", f"{la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}", w))
    sd = c.get("sensors")
    fans = sd.get("fans") if isinstance(sd, dict) else None
    if fans:
        lines.append(kv("Fan", fan_text(fans), w))
    lines.append(kv("Trend", spark(c.cpu_hist, max(w - LABEL_W, 4), 100, "blue"), w))
    return lines, level


def tile_gpu(c, w):
    g = c.get("gpu")
    if g is LOADING:
        return loading()
    if not g:
        return [txt("no GPU detected", "faint")], 0
    lines = [split(txt(g["name"], bold=True), txt(f"({g['kind']})", "faint"), w)]
    level = 0
    if g["usage"] is not None:
        lines.append(bar_row("Usage", g["usage"], txt(f"{g['usage']:>3.0f}%"), w))
        level = sev(g["usage"])
    else:
        lines.append(kv("Usage", "–", w, "faint"))
    if g["temp"] is not None:
        col = scolor(g["temp"], 70, 85)
        lines.append(kv("Temp", txt(f"{g['temp']:.0f}°C", col), w))
        level = max(level, sev(g["temp"], 70, 85))
    else:
        lines.append(kv("Temp", "–", w, "faint"))
    lines.append(kv("Clock", f"{g['clock']:.0f} MHz" if g["clock"] else "–", w,
                    "bright" if g["clock"] else "faint"))
    if g["vram"]:
        used, total = g["vram"]
        lines.append(bar_row("VRAM", used / total * 100 if total else 0,
                             txt(f"{human_gib(used)[:-4]}/{human_gib(total)}"), w, color=T["mauve"]))
    elif g["kind"] == "integrated":
        lines.append(kv("Memory", "shared", w))
    if g["driver"]:
        lines.append(kv("Driver", g["driver"], w))
    return lines, level


def tile_ram(c, w):
    vm = psutil.virtual_memory()
    used = vm.total - vm.available
    pct = used / vm.total * 100
    lines = [split(txt(f"{human_gib(used)[:-4]} / {human_gib(vm.total)}", bold=True),
                   txt(f"{pct:.0f}%", scolor(pct), bold=True), w),
             bar_row("Used", pct, txt(human_gib(used)), w),
             kv("Avail", human_gib(vm.available), w),
             kv("Cached", human_gib(getattr(vm, "cached", 0)), w)]
    sw = psutil.swap_memory()
    if sw.total:
        lines.append(bar_row("Swap", sw.percent, txt(f"{human_size(sw.used)}/{human_size(sw.total)}"),
                             w, color=T["peach"] if sw.percent < 90 else T["crit"]))
    else:
        lines.append(kv("Swap", "none", w, "faint"))
    level = sev(pct, 80, 92)
    mp = read_pressure("memory")
    if mp and "some" in mp:
        t = txt(f"{mp['some']:.1f}%", scolor(mp["some"], 10, 30))
        if "full" in mp:
            t.append(f"  full {mp['full']:.1f}%", style=T["faint"])
        lines.append(kv("Pressure", t, w))
        level = max(level, sev(mp["some"], 10, 30))
    if sw.total:
        lines.append(kv("Swap I/O", Text.assemble(
            ("in ", T["muted"]), (f"{human_size(c.swap_in)}/s", T["bright"]),
            ("  out ", T["muted"]), (f"{human_size(c.swap_out)}/s", T["bright"])), w))
    return lines, level


def tile_storage(c, w):
    drive = c.get("drive")
    lines, level = [], 0
    if drive and drive.get("model"):
        lines.append(split(txt(drive["model"], bold=True), txt(drive.get("size") or "", "muted"), w))
    mounts = list_mounts()
    for p in mounts[:4]:
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        mp = "/efi" if p.mountpoint == "/boot/efi" else p.mountpoint
        mp = mp if len(mp) < LABEL_W else "…" + mp[-(LABEL_W - 2):]
        lines.append(bar_row(mp, u.percent, txt(f"{human_size(u.used)}/{human_size(u.total)}"), w))
        level = max(level, sev(u.percent, 85, 95))
    if len(mounts) > 4:
        lines.append(txt(f"+{len(mounts) - 4} more", "faint"))
    if c.has_disk_io:
        lines.append(rate_row("R", c.disk_r_hist, c.disk_r, "teal", w))
        lines.append(rate_row("W", c.disk_w_hist, c.disk_w, "mauve", w))
    if drive and drive.get("kind"):
        row = txt(f"{drive['kind']} · {drive['fstype']}")
        sd = c.get("sensors")
        dtemp = sd.get("drive_temp") if isinstance(sd, dict) else None
        if dtemp is not None:
            row.append(f"  {dtemp:.0f}°C", style=scolor(dtemp, 70, 80))
            level = max(level, sev(dtemp, 70, 80))
        lines.append(kv("Drive", row, w))
    return (lines or [txt("no disks found", "faint")]), level


def signal_text(n):
    col = T["ok"] if n >= 3 else T["warn"] if n == 2 else T["crit"]
    t = Text()
    for i, ch in enumerate("▂▄▆█"):
        t.append(ch, style=col if i < n else T["empty"])
    return t


def rate_row(arrow, hist, rate, color, w):
    rs = human_rate(rate).rjust(10)
    sw = max(w - 2 - 10 - 1, 4)
    t = Text(f"{arrow} ", style=T[color])
    t.append_text(spark(hist, sw, max(max(hist, default=0), 1024), color))
    t.append(" ")
    t.append(rs, style=T["bright"])
    return t


def tile_network(c, w):
    if not c.iface:
        return [txt("offline", "crit", bold=True), kv("Status", "no active interface", w, "faint")], 2
    wifi = os.path.isdir(f"/sys/class/net/{c.iface}/wireless")
    right = txt("")
    if wifi:
        bars = wifi_signal(c.iface)
        right = signal_text(bars) if bars else txt("")
    else:
        speed = fnum(read(f"/sys/class/net/{c.iface}/speed"))
        right = txt(f"{speed:.0f} Mb/s" if speed and speed > 0 else "wired", "muted")
    lines = [split(txt(f"{c.iface}  {'Wi-Fi' if wifi else 'Ethernet'}", bold=True), right, w)]
    if wifi:
        ssid = c.get("ssid")
        if ssid not in (None, LOADING):
            lines.append(kv("SSID", ssid, w))
    if c.public_ip:
        pub = c.get("pubip")
        lines.append(kv("Pub. IP", "…" if pub is LOADING else (pub or "–"), w,
                        "bright" if pub not in (None, LOADING) else "faint"))
    ip = "–"
    for a in psutil.net_if_addrs().get(c.iface, []):
        if a.family == socket.AF_INET:
            ip = a.address
    lines.append(kv("Local IP", ip, w))
    lines.append(rate_row("↓", c.down_hist, c.down, "blue", w))
    lines.append(rate_row("↑", c.up_hist, c.up, "peach", w))
    if c.net_total:
        lines.append(kv("Total", f"↓ {human_gib(c.net_total[0])}  ↑ {human_gib(c.net_total[1])}", w))
    vendor = c.get("nic")
    if vendor not in (None, LOADING):
        lines.append(kv("Vendor", vendor, w))
    return lines, 0


def tile_os(c, w):
    osr = {}
    for line in (read("/etc/os-release", "") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k] = v.strip('"')
    de = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "–"
    session = os.environ.get("XDG_SESSION_TYPE")
    if session and de != "–":
        de += f" ({session.capitalize()})"
    shell = c.get("shell")
    pkgs = c.get("pkgs")
    lines = [txt(osr.get("PRETTY_NAME") or platform.system(), bold=True),
             kv("Kernel", platform.release(), w),
             kv("Shell", shell if shell not in (None, LOADING) else "–", w),
             kv("DE", de, w)]
    if pkgs not in (None, LOADING):
        lines.append(kv("Pkgs", pkgs, w))
    level = 0
    if "updates" in c.slow_jobs:
        up = c.get("updates")
        if up is LOADING:
            lines.append(kv("Updates", "checking…", w, "faint"))
        elif up is not None:
            lines.append(kv("Updates", txt("up to date", "ok") if up == 0 else txt(f"{up} available"), w))
    if "services" in c.slow_jobs:
        failed = c.get("services")
        if failed is LOADING:
            lines.append(kv("Services", "checking…", w, "faint"))
        elif failed:
            t = txt(f"{len(failed)} failed", "warn", bold=True)
            t.append("  " + ", ".join(failed), style=T["faint"])
            lines.append(kv("Services", t, w))
            level = 1
        elif failed is not None:
            lines.append(kv("Services", "all OK", w, "ok"))
    if c.get("reboot") is True:
        lines.append(kv("Reboot", badge("REQUIRED", "warn"), w))
    return lines, level


def tile_processes(c, w):
    la = os.getloadavg()
    lines = [split(txt(f"Load {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}", bold=True),
                   txt(f"{c.ntasks} tasks", "muted"), w)]
    nw = max(w - 21, 6)
    lines.append(txt(f"{'Name':<{nw}} {'PID':>7} {'CPU%':>5} {'MEM':>6}", "muted"))
    for cpu, pid, name, rss in c.top[:5]:
        row = Text(f"{name[:nw]:<{nw}} ", style=T["bright"])
        row.append(f"{pid:>7} ", style=T["muted"])
        row.append(f"{cpu:>5.1f}", style=scolor(cpu, 50, 80))
        row.append(f" {human_size(rss):>6}", style=T["bright"])
        lines.append(row)
    return lines, 0


def tile_peripherals(c, w):
    usb, bt = c.get("usb"), c.get("bt")
    if usb is LOADING or bt is LOADING:
        return loading()
    items = [(n, txt("USB", "muted")) for n in (usb or [])]
    for name, batt in bt or []:
        tag = Text("BT", style=T["muted"])
        if batt is not None:
            tag = Text(f"BT {batt}%", style=T["crit"] if batt < 15 else T["warn"] if batt < 30 else T["muted"])
        items.append((name, tag))
    if not items:
        return [txt("none detected", "faint")], 0
    lines = [split(txt(n), tag, w) for n, tag in items[:5]]
    if len(items) > 5:
        lines.append(txt(f"+{len(items) - 5} more", "faint"))
    return lines, 0


def tile_audio(c, w):
    a = c.get("audio")
    if a is LOADING:
        return loading()
    if not a:
        return [txt("no audio server found", "faint")], 0
    muted = a["muted"]
    lines = [kv("Out", a["out"] or "–", w)]
    if a["vol"] is not None:
        lines.append(bar_row("Vol", a["vol"], txt(f"{a['vol']:>3}%", "faint" if muted else "bright"), w,
                             color=T["faint"] if muted else T["blue"]))
    lines.append(kv("In", a["inp"] or "–", w))
    lines.append(kv("Mute", badge("MUTED", "crit") if muted else txt("off", "faint"), w))
    return lines, 0


def tile_display(c, w):
    mons = c.get("display")
    if mons is LOADING:
        return loading()
    if not mons:
        return [txt("no display detected", "faint")], 0
    m = mons[0]
    res = (m["mode"] or "–").replace("x", "×")
    lines = [split(txt(res, bold=True), txt(f"{m['rate']:.0f} Hz" if m["rate"] else "", "muted"), w)]
    panel = " ".join(x for x in (m["vendor"], m["model"]) if x)
    lines.append(kv("Panel", panel or m["name"], w))
    lines.append(kv("Screens", str(len(mons)), w))
    lines.append(kv("Session", (os.environ.get("XDG_SESSION_TYPE") or "–").capitalize(), w))
    for extra in mons[1:3]:
        lines.append(kv(extra["name"][:8], f"{(extra['mode'] or '–').replace('x', '×')}", w))
    return lines, 0


def tile_battery(c, w):
    b = battery_info()
    if not b:
        return None
    pct, status = b["pct"], b["status"]
    col = T["ok"] if pct > 30 else T["warn"] if pct > 15 else T["crit"]
    head = txt(f"{pct:>3.0f}% ", col, bold=True)
    head.append_text(bar(pct, max(w - head.cell_len, 4), col))
    symbol = {"Charging": "▲ Charging", "Discharging": "▼ Discharging", "Full": "● Full",
              "Not charging": "– Not charging"}.get(status, status)
    lines = [head, kv("Status", txt(symbol, "ok" if status == "Charging" else "bright"), w)]
    if b["left"]:
        h, m = divmod(int(b["left"]) // 60, 60)
        lines.append(kv("Left", f"{h}h {m:02d}m", w))
    if b["watts"]:
        lines.append(kv("Power", f"{b['watts']:.1f} W", w))
    if b["health"]:
        lines.append(kv("Health", txt(f"{b['health']:.0f}%", scolor(100 - b["health"], 20, 35)), w))
    if b["cycles"] is not None:
        lines.append(kv("Cycles", f"{b['cycles']:.0f}", w))
    level = 2 if (pct <= 15 and status == "Discharging") else 1 if (pct <= 30 and status == "Discharging") else 0
    return lines, level


TILES = [
    ("Machine", tile_machine), ("CPU", tile_cpu), ("GPU", tile_gpu), ("RAM", tile_ram),
    ("Storage", tile_storage), ("Network", tile_network), ("OS", tile_os),
    ("Processes", tile_processes), ("Peripherals", tile_peripherals), ("Audio", tile_audio),
    ("Display", tile_display), ("Battery", tile_battery),
]


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def bar_table():
    t = Table(show_header=False, show_edge=False, box=None, expand=True,
              padding=(0, 1), style=f"on {T['bar']}")
    t.add_column(ratio=1, justify="left")
    t.add_column(ratio=1, justify="center")
    t.add_column(ratio=1, justify="right")
    return t


def header():
    up = time.time() - psutil.boot_time()
    d, h = int(up // 86400), int(up % 86400 // 3600)
    up_s = f"{d}d {h}h" if d else f"{h}h {int(up % 3600 // 60)}m"
    t = bar_table()
    t.add_row(txt(f"{getpass.getuser()}@{socket.gethostname()}", bold=True),
              txt(TOOL_NAME, "title", bold=True),
              txt(f"{datetime.now():%H:%M:%S}  up {up_s}", "muted"))
    return t


def footer(interval, theme):
    t = bar_table()
    hint = Text.assemble(("Ctrl+C", f"bold {T['peach']}"), (" quit", T["muted"]))
    t.add_row(hint, Text(""), txt(f"every {interval:g}s · {theme}", "faint"))
    return t


def make_panel(tile, width, height, limit):
    name, lines, level = tile
    lines = lines[:limit] if limit else lines
    for ln in lines:
        ln.no_wrap = True
        ln.overflow = "ellipsis"
    border = (T["border"], T["warn"], T["crit"])[level]
    return Panel(Group(*lines), title=Text(name, style=f"bold {T['title']}"), title_align="left",
                 border_style=border, box=box.ROUNDED, padding=(0, 1), width=width, height=height)


def render(c, console, interval, theme, fill):
    width, height = console.size
    cols = 4 if width >= 128 else 3 if width >= 96 else 2 if width >= 64 else 1
    base, extra = divmod(width - (cols - 1), cols)
    widths = [base + (1 if i < extra else 0) for i in range(cols)]
    tiles = []
    for name, fn in TILES:
        w = widths[len(tiles) % cols] - 4
        try:
            res = fn(c, w)
        except Exception:
            res = ([txt("unavailable", "faint")], 0)
        if res is not None:
            tiles.append((name, res[0], res[1]))
    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    avail = height - 2
    limit = None
    for lim in (None, 8, 6, 5, 4, 3):
        limit = lim
        need = sum(min(max(len(t[1]) for t in r), lim or 99) + 2 for r in rows)
        if need <= avail:
            break
    heights = [min(max(len(t[1]) for t in r), limit or 99) + 2 for r in rows]
    if fill:
        spare = min(max(avail - sum(heights), 0) // len(rows), 3)
        heights = [h + spare for h in heights]
    grid = Table.grid(padding=0)
    for i, wd in enumerate(widths):
        if i:
            grid.add_column(width=1)
        grid.add_column(width=wd)
    for r, h in zip(rows, heights):
        cells = []
        for i in range(cols):
            if i:
                cells.append("")
            cells.append(make_panel(r[i], widths[i], h, limit) if i < len(r) else "")
        grid.add_row(*cells)
    return Group(header(), grid, footer(interval, theme))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(prog="sysinfo", description="Tile-based system info dashboard.")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--theme", choices=list(THEMES), default="mocha")
    ap.add_argument("--public-ip", action="store_true",
                    help="look up public IP via api.ipify.org (off by default)")
    args = ap.parse_args()
    args.interval = max(args.interval, 0.5)
    T.clear()
    T.update(THEMES[args.theme])

    console = Console()
    c = Collector(public_ip=args.public_ip)

    if args.once or not sys.stdout.isatty():
        time.sleep(0.5)
        c.sample()
        time.sleep(0.5)
        c.sample()
        c.refresh_slow(force=True)
        console.print(render(c, console, args.interval, args.theme, fill=False))
        return 0

    c.start_background()
    time.sleep(0.3)
    try:
        with Live(console=console, screen=True, auto_refresh=False, vertical_overflow="crop") as live:
            while True:
                c.sample()
                live.update(render(c, console, args.interval, args.theme, fill=True), refresh=True)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())