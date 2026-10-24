#!/usr/bin/env python3
"""sysinfo: a tile-based system information dashboard for the Linux terminal.

    sysinfo.py                  live dashboard (refreshes every 2s), Ctrl+C to exit
    sysinfo.py --once           print one snapshot and exit
    sysinfo.py -i 1 --theme tokyo
    sysinfo.py --public-ip      look up + show your public IP at start (otherwise it stays
                                hidden as a spoiler; reveal it with a click or `p`)
    sysinfo.py --disk sda       start the Storage tile on a specific drive
    sysinfo.py --no-mouse       don't capture the mouse (keeps normal text selection)

Live mode controls:
    mouse wheel      scroll the tile under the pointer (Processes, Peripherals;
                     on Storage it switches drive)
    click            focus a tile (blue border); click the Processes column header
                     row to toggle CPU / memory sorting
    j / k, Up / Down, PgUp / PgDn   scroll the focused tile (Processes by default)
    s                toggle process sort (CPU <-> memory)
    p, or click the Pub. IP row   reveal / hide the public IP (looked up only on first reveal)
    [ and ]  (or Tab / Left / Right)   switch drive in the Storage tile
    q or Ctrl+C      quit
With mouse capture on, hold Shift to select text in most terminals.

Dependencies (Debian):  sudo apt install python3-rich python3-psutil
Optional tools that unlock more info: iw (Wi-Fi monitor-mode support), resolvectl,
nmcli, gdbus (GNOME display scale), lspci, nvidia-smi.
"""
from __future__ import annotations

import argparse
import getpass
import glob
import json
import os
import platform
import re
import select
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
from functools import lru_cache

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

# Tools like iw, lspci or resolvectl often live in /usr/sbin, which a normal user's PATH
# may not include. Search there too so the dashboard works however it was launched.
SEARCH_PATH = os.environ.get("PATH", "") + os.pathsep + "/usr/sbin:/sbin:/usr/local/sbin"


def which(cmd):
    return shutil.which(cmd, path=SEARCH_PATH)


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

# Tiles whose content scrolls. Value = default line budget before the first layout pass.
FLEX = {"Processes": 7, "Peripherals": 5}


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
    exe = which(cmd[0])
    if not exe:
        return None
    try:
        r = subprocess.run([exe] + list(cmd[1:]), capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "LC_ALL": "C"})
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def run_rc(cmd, timeout=1.5):
    """Like run(), but returns (exit_code, stdout) so callers can accept non-zero codes."""
    exe = which(cmd[0])
    if not exe:
        return None
    try:
        r = subprocess.run([exe] + list(cmd[1:]), capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "LC_ALL": "C"})
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


@lru_cache(maxsize=1)
def cpu_flags():
    for line in (read("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith(("flags", "Features")):
            return frozenset(line.split(":", 1)[1].split())
    return frozenset()


def virt_support():
    """Hardware virtualization support: VT-x / AMD-V flag, KVM device, IOMMU, or 'inside a VM'."""
    flags = cpu_flags()
    iommu = False
    try:
        iommu = bool(os.listdir("/sys/class/iommu"))
    except OSError:
        pass
    return dict(hw="VT-x" if "vmx" in flags else "AMD-V" if "svm" in flags else None,
                kvm=os.path.exists("/dev/kvm"),
                kvm_ok=os.access("/dev/kvm", os.R_OK | os.W_OK),
                iommu=iommu, in_vm="hypervisor" in flags)


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
    if which("nvidia-smi"):
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


# ---- network -------------------------------------------------------------- #
VPN_RE = re.compile(r"^(tun|wg|ppp|tailscale|zt|nordlynx|proton|mullvad|ipsec|vpn|utun|cscotun)", re.I)


def is_vpn_iface(name):
    """Tunnel interfaces: known VPN names, or L3 devices with no link layer (type 65534)."""
    return bool(VPN_RE.match(name)) or read(f"/sys/class/net/{name}/type") == "65534"


def default_iface():
    """The physical interface carrying the default route (a VPN tunnel is skipped, so the
    Network tile keeps describing your real Wi-Fi / Ethernet link)."""
    routes = []
    for line in (read("/proc/net/route", "") or "").splitlines()[1:]:
        f = line.split()
        if len(f) > 6 and f[1] == "00000000" and int(f[3], 16) & 2:
            routes.append((int(f[6]), f[0]))
    routes.sort()
    for _, name in routes:
        if not is_vpn_iface(name):
            return name
    if routes:
        return routes[0][1]
    try:
        for name, st in psutil.net_if_stats().items():
            if st.isup and name != "lo" and not is_vpn_iface(name):
                return name
    except Exception:
        pass
    return None


def vpn_info():
    """Active VPN connections: NetworkManager names when available, tunnel interfaces otherwise."""
    names = []
    out = run(["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show", "--active"], timeout=2)
    for ln in (out or "").splitlines():
        kind, _, name = ln.partition(":")
        if kind in ("vpn", "wireguard") and name:
            names.append(name)
    try:
        stats = psutil.net_if_stats()
    except Exception:
        stats = {}
    ifaces = [n for n, s in stats.items() if s.isup and n != "lo" and is_vpn_iface(n)]
    return dict(active=bool(names or ifaces), names=names or ifaces)


def dns_servers(iface):
    servers = []
    if iface:
        out = run(["resolvectl", "dns", iface])
        m = re.search(r"\):\s*(.*)$", (out or "").strip())
        if m:
            servers = [t.split("%")[0] for t in m.group(1).split()]
    if not servers:
        for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
            text = read(path)
            if text:
                servers = [ln.split()[1] for ln in text.splitlines()
                           if ln.startswith("nameserver") and len(ln.split()) > 1]
                if servers:
                    break
    return servers


def wifi_monitor_support(iface):
    """True/False if the Wi-Fi driver advertises monitor mode, None if it can't be determined.
    Needs the `iw` tool. This is a capability check only; it says nothing about injection."""
    if not iface:
        return None
    phy = read(f"/sys/class/net/{iface}/phy80211/name")
    if not phy:
        return None
    out = run(["iw", "phy", phy, "info"], timeout=2)
    if not out:
        return None
    m = re.search(r"Supported interface modes:\n((?:[ \t]+\*.*\n)+)", out)
    return ("monitor" in m.group(1)) if m else None


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


# ---- storage -------------------------------------------------------------- #
DISK_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")


def list_mounts():
    seen = {}
    for p in psutil.disk_partitions(all=False):
        mp = p.mountpoint
        if (p.fstype in ("squashfs", "iso9660") or p.device.startswith("/dev/loop")
                or mp.startswith(("/snap", "/var/snap", "/var/lib/docker"))
                or (mp.startswith("/run") and not mp.startswith("/run/media"))):
            continue
        if p.device in seen and len(mp) >= len(seen[p.device].mountpoint):
            continue
        seen[p.device] = p
    return sorted(seen.values(), key=lambda p: (p.mountpoint != "/", p.mountpoint))


def disk_of(name, depth=0):
    """Physical disk behind a block device: partitions climb to their parent,
    LUKS/LVM/RAID layers follow their slaves."""
    p = f"/sys/class/block/{name}"
    if not os.path.exists(p):
        return None
    if os.path.exists(p + "/partition"):
        return os.path.basename(os.path.dirname(os.path.realpath(p)))
    if depth < 4 and os.path.isdir(p + "/slaves"):
        for s in sorted(os.listdir(p + "/slaves")):
            r = disk_of(s, depth + 1)
            if r:
                return r
    return name if os.path.exists(p + "/device") else None


def list_disks():
    """Physical drives (root drive first), each with its model, size, kind and mounted partitions."""
    by_disk, root_disk = {}, None
    for p in list_mounts():
        d = disk_of(os.path.basename(os.path.realpath(p.device)))
        if d is None:
            continue
        by_disk.setdefault(d, []).append(p)
        if p.mountpoint == "/":
            root_disk = d
    disks = []
    for path in sorted(glob.glob("/sys/block/*")):
        name = os.path.basename(path)
        if not DISK_RE.match(name):
            continue
        sectors = fnum(read(path + "/size")) or 0
        if sectors <= 0:
            continue
        model = read(path + "/device/model")
        if name.startswith("nvme"):
            kind = "NVMe SSD"
        elif name.startswith("mmcblk"):
            kind = "SD / eMMC"
        else:
            kind = "HDD" if read(path + "/queue/rotational") == "1" else "SSD"
        try:
            nparts = len([x for x in os.listdir(path) if os.path.exists(f"{path}/{x}/partition")])
        except OSError:
            nparts = 0
        disks.append(dict(name=name, model=re.sub(r"\s+", " ", model) if model else None,
                          size=sectors * 512, kind=kind, usb="/usb" in os.path.realpath(path),
                          nparts=nparts, mounts=by_disk.get(name, []), is_root=(name == root_disk)))
    disks.sort(key=lambda d: (not d["is_root"], d["name"]))
    return disks


def mount_label(mp):
    mp = "/efi" if mp == "/boot/efi" else mp
    return mp if len(mp) < LABEL_W else "…" + mp[-(LABEL_W - 2):]


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


# ---- misc ----------------------------------------------------------------- #
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
    if which("pactl"):
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
            # Mic input gain + mute. A ".monitor" source is a loopback of the speakers, not a mic.
            is_monitor = source.endswith(".monitor")
            mic_vol = mic_muted = None
            if source and not is_monitor:
                mv = re.search(r"(\d+)%", run(["pactl", "get-source-volume", "@DEFAULT_SOURCE@"]) or "")
                mic_vol = int(mv.group(1)) if mv else None
                mic_muted = "yes" in (run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"]) or "")
            return dict(out=desc.get(sink, sink), vol=int(vol.group(1)) if vol else None,
                        muted=muted, inp=desc.get(source, source) or None,
                        mic_vol=mic_vol, mic_muted=mic_muted, mic_monitor=is_monitor)
    out = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    if out:
        m = re.search(r"([\d.]+)", out)

        def node(which_node):
            info = run(["wpctl", "inspect", which_node]) or ""
            d = re.search(r'node\.description = "(.*)"', info)
            return d.group(1) if d else None

        mic_out = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"])
        mm = re.search(r"([\d.]+)", mic_out or "")
        return dict(out=node("@DEFAULT_AUDIO_SINK@") or "default output",
                    vol=round(float(m.group(1)) * 100) if m else None,
                    muted="MUTED" in out, inp=node("@DEFAULT_AUDIO_SOURCE@"),
                    mic_vol=round(float(mm.group(1)) * 100) if mm else None,
                    mic_muted=("MUTED" in mic_out) if mic_out else None, mic_monitor=False)
    return None


# ---- display -------------------------------------------------------------- #
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


def brightness():
    """Backlight level in %, from the backlight device with the finest scale. None on desktops."""
    best = None
    for d in sorted(glob.glob("/sys/class/backlight/*")):
        cur, mx = fnum(read(d + "/brightness")), fnum(read(d + "/max_brightness"))
        if cur is not None and mx and (best is None or mx > best[1]):
            best = (cur, mx)
    return best[0] / best[1] * 100 if best else None


def display_scales():
    """Output name -> scale factor (1.0 = 100%). Tries GNOME (Mutter), KDE, Sway, Hyprland,
    then falls back to the X11 DPI / GDK_SCALE as a single '_global' value."""
    res = {}
    out = run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
               "--object-path", "/org/gnome/Mutter/DisplayConfig",
               "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"], timeout=2)
    if out:
        for m in re.finditer(r"\((-?\d+), (-?\d+), ([\d.]+), uint32 \d+, (?:true|false), \[\('([^']+)'", out):
            res[m.group(4)] = float(m.group(3))
        if res:
            return res
    out = run(["kscreen-doctor", "-o"], timeout=2)
    if out:
        cur = None
        for line in re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines():
            m = re.match(r"\s*Output:\s+\d+\s+(\S+)", line)
            if m:
                cur = m.group(1)
            m = re.match(r"\s*Scale:\s*([\d.]+)", line)
            if m and cur:
                res[cur] = float(m.group(1))
        if res:
            return res
    for cmd in (["swaymsg", "-t", "get_outputs", "-r"], ["hyprctl", "monitors", "-j"]):
        out = run(cmd, timeout=2)
        if not out:
            continue
        try:
            for o in json.loads(out):
                if o.get("scale") and o.get("name"):
                    res[o["name"]] = float(o["scale"])
        except (ValueError, TypeError, AttributeError):
            pass
        if res:
            return res
    if os.environ.get("DISPLAY"):
        m = re.search(r"Xft\.dpi:\s*(\d+)", run(["xrdb", "-query"]) or "")
        if m:
            return {"_global": float(m.group(1)) / 96}
    gdk = fnum(os.environ.get("GDK_SCALE"))
    return {"_global": gdk} if gdk else {}


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
    scales = display_scales()
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
                         vendor=vendor, model=model,
                         scale=scales.get(name) or scales.get("_global")))
    return mons


# ---- battery -------------------------------------------------------------- #
def battery_info():
    """Raw battery readings. `rate` is power (µW) when the battery reports energy, or current (µA)
    when it reports charge, always in the same unit as `now` / `full`."""
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
    current = fnum(read(b + "/current_now"))
    if power is None and current is not None and volt is not None:
        power = abs(current) * volt / 1e6
    en, ef = fnum(read(b + "/energy_now")), fnum(read(b + "/energy_full"))
    if en is not None and ef is not None:
        now, full, rate = en, ef, (abs(power) if power else None)
        design = fnum(read(b + "/energy_full_design"))
    else:
        now, full = fnum(read(b + "/charge_now")), fnum(read(b + "/charge_full"))
        rate = abs(current) if current else None
        design = fnum(read(b + "/charge_full_design"))
    kernel_left = fnum(read(b + ("/time_to_full_now" if status == "Charging" else "/time_to_empty_now")))
    return dict(pct=cap, status=status, watts=power / 1e6 if power else None,
                now=now, full=full, rate=rate,
                kernel_left=kernel_left if kernel_left and kernel_left > 0 else None,
                health=full / design * 100 if full and design else None,
                cycles=fnum(read(b + "/cycle_count")))


def battery_eta(b, rates):
    """Seconds until full (charging) or empty (discharging), smoothed over recent samples.
    Charging estimates run optimistic near 100% because the charger tapers off."""
    if not b or b["status"] not in ("Charging", "Discharging"):
        return None
    if b["kernel_left"]:
        return b["kernel_left"]
    if b["now"] is None or b["full"] is None or not rates:
        return None
    rate = sum(rates) / len(rates)
    remaining = (b["full"] - b["now"]) if b["status"] == "Charging" else b["now"]
    if rate <= 0 or remaining <= 0:
        return None
    return remaining / rate * 3600


# ---- sensors / system health ---------------------------------------------- #
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


def fan_speeds():
    fn = getattr(psutil, "sensors_fans", None)
    try:
        data = fn() if fn else {}
    except Exception:
        return []
    return [e.current for entries in (data or {}).values() for e in entries]


def pending_updates():
    """Number of upgradable packages according to the local package lists (no network,
    no root). Returns None if the package manager isn't supported or the check fails."""
    if which("apt-get"):
        res = run_rc(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=30)
        if res and res[0] == 0:
            return sum(1 for ln in res[1].splitlines() if ln.startswith("Inst "))
        return None
    if which("pacman"):
        res = run_rc(["pacman", "-Qu"], timeout=15)
        if res is None:
            return None
        rc, out = res
        if rc == 0:
            return len([ln for ln in out.splitlines() if ln.strip()])
        return 0 if rc == 1 and not out.strip() else None
    if which("dnf"):
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


UPDATES_SUPPORTED = any(which(x) for x in ("apt-get", "pacman", "dnf"))


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
# Keyboard + mouse input (live mode only)
# --------------------------------------------------------------------------- #
# Events are (kind, value, x, y) tuples. kinds: next prev quit sort line scroll click
_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_KEY_RE = re.compile(r"\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[[0-9;]*[A-Za-z~]|\x1bO.|.", re.S)
_KEYMAP = {"]": ("next", 0), ".": ("next", 0), "\t": ("next", 0),
           "\x1b[C": ("next", 0), "\x1bOC": ("next", 0),
           "[": ("prev", 0), ",": ("prev", 0), "\x1b[Z": ("prev", 0),
           "\x1b[D": ("prev", 0), "\x1bOD": ("prev", 0),
           "q": ("quit", 0), "Q": ("quit", 0),
           "s": ("sort", 0), "S": ("sort", 0), "p": ("pub", 0), "P": ("pub", 0),
           "j": ("line", 1), "\x1b[B": ("line", 1), "\x1bOB": ("line", 1),
           "k": ("line", -1), "\x1b[A": ("line", -1), "\x1bOA": ("line", -1),
           "\x1b[6~": ("line", 5), "\x1b[5~": ("line", -5)}


def parse_keys(data):
    out = []
    for t in _KEY_RE.findall(data):
        m = _MOUSE_RE.fullmatch(t)
        if m:
            b, x, y = int(m[1]), int(m[2]) - 1, int(m[3]) - 1
            if b & 64:                                   # wheel: 64 = up, 65 = down
                out.append(("scroll", 1 if b & 1 else -1, x, y))
            elif (b & 3) == 0 and not (b & 32) and m[4] == "M":   # left button press
                out.append(("click", 0, x, y))
        elif t in _KEYMAP:
            kind, val = _KEYMAP[t]
            out.append((kind, val, 0, 0))
    return out


class Keys:
    """Non-blocking key/mouse reader. Uses cbreak mode, so Ctrl+C still raises KeyboardInterrupt,
    and always restores the terminal (including mouse reporting) on exit."""

    def __init__(self, mouse=True):
        self.fd = None
        self._old = None
        self.want_mouse = mouse
        self.mouse = False

    @property
    def active(self):
        return self.fd is not None

    def __enter__(self):
        try:
            if sys.stdin.isatty():
                import termios
                import tty
                self.fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
                if self.want_mouse:
                    sys.stdout.write("\x1b[?1000h\x1b[?1006h")   # button events + SGR encoding
                    sys.stdout.flush()
                    self.mouse = True
        except Exception:
            self.fd = None
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            import termios
            if self.mouse:
                sys.stdout.write("\x1b[?1006l\x1b[?1000l")
                sys.stdout.flush()
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old)

    def wait(self, timeout):
        """Wait up to `timeout` seconds; return a list of (kind, value, x, y) events."""
        if self.fd is None:
            time.sleep(max(timeout, 0))
            return []
        ready, _, _ = select.select([self.fd], [], [], max(timeout, 0))
        if not ready:
            return []
        return parse_keys(os.read(self.fd, 512).decode("utf-8", "ignore"))


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
        self.disks = []
        self.disk_sel = None
        self.disk_io = {}
        self.swap_in = self.swap_out = 0.0
        self._swap_last = None
        self.battery = None
        self.batt_eta = None
        self.rate_hist = deque(maxlen=20)
        self._batt_status = None
        self._procs = []
        self.top = []
        self.ntasks = 0
        self.sort = "cpu"
        # interaction state
        self.focus = None
        self.proc_off = 0
        self.periph_off = 0
        self.hitboxes = []
        self.flex = dict(FLEX)
        self.tile_cache = {}
        self.pub_reveal = public_ip       # public IP is a spoiler unless --public-ip
        self.pub_row = None
        self.mouse_on = False
        self._bg = False
        self.cpu_name = cpu_model()
        self.ncpu = psutil.cpu_count() or 1
        self._slow = {}
        self._slow_t = {}
        self.public_ip = public_ip
        self.slow_jobs = {
            "gpu": (gpu_info, 2), "audio": (audio_info, 3), "bt": (bluetooth_devices, 10),
            "usb": (usb_devices, 5), "display": (display_info, 15), "ssid": (wifi_ssid, 10),
            "pkgs": (count_packages, 3600), "shell": (shell_name, 3600),
            "nic": (lambda: nic_vendor(self.iface), 60),
            "dns": (lambda: dns_servers(self.iface), 30),
            "vpn": (vpn_info, 10),
            "monmode": (lambda: wifi_monitor_support(self.iface), 60),
            "sensors": (self._sensors, 2), "reboot": (reboot_required, 30),
        }
        if which("systemctl"):
            self.slow_jobs["services"] = (failed_units, 30)
        if UPDATES_SUPPORTED:
            self.slow_jobs["updates"] = (pending_updates, 1800)
        if public_ip:
            self.slow_jobs["pubip"] = (fetch_public_ip, 300)
        psutil.cpu_percent(None)

    # -- drive selection (Storage tile) --
    def selected_disk(self):
        for d in self.disks:
            if d["name"] == self.disk_sel:
                return d
        return self.disks[0] if self.disks else None

    def cycle_disk(self, step):
        cur = self.selected_disk()
        if not cur or len(self.disks) < 2:
            return
        names = [d["name"] for d in self.disks]
        self.disk_sel = names[(names.index(cur["name"]) + step) % len(names)]

    # -- interaction --
    def tile_at(self, x, y):
        for box in self.hitboxes:
            _, x0, y0, x1, y1 = box
            if x0 <= x < x1 and y0 <= y < y1:
                return box
        return None

    def start_pub(self):
        """Register the public-IP lookup the first time it's needed (nothing is sent before)."""
        if "pubip" in self.slow_jobs:
            return
        self.slow_jobs["pubip"] = (fetch_public_ip, 300)
        if self._bg:
            self._spawn("pubip")

    def toggle_pub(self):
        self.pub_reveal = not self.pub_reveal
        if self.pub_reveal:
            self.start_pub()
        self.tile_cache.pop("Network", None)     # the Network tile is cached while scrolling

    def toggle_sort(self):
        self.sort = "mem" if self.sort == "cpu" else "cpu"
        self.proc_off = 0
        self._sort_top()

    def scroll(self, name, step):
        if name == "Processes":
            self.proc_off += step
        elif name == "Peripherals":
            self.periph_off += step
        elif name == "Storage":
            self.cycle_disk(1 if step > 0 else -1)

    def handle(self, kind, val, x=0, y=0):
        if kind in ("next", "prev"):
            self.cycle_disk(1 if kind == "next" else -1)
        elif kind == "sort":
            self.toggle_sort()
        elif kind == "pub":
            self.toggle_pub()
        elif kind == "click":
            box = self.tile_at(x, y)
            self.focus = box[0] if box else None
            # Row y0+2 of the Processes tile is its column-header line.
            if box and box[0] == "Processes" and y == box[2] + 2:
                self.toggle_sort()
            elif (box and box[0] == "Network" and self.pub_row is not None
                  and y == box[2] + 1 + self.pub_row):
                self.toggle_pub()
        elif kind == "scroll":
            box = self.tile_at(x, y)
            if box:
                self.scroll(box[0], val)
        elif kind == "line":
            self.scroll(self.focus or "Processes", val)

    # -- slow data --
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
        return dict(fans=fan_speeds())

    def _spawn(self, key):
        fn, ttl = self.slow_jobs[key]

        def loop():
            while True:
                try:
                    self._slow[key] = fn()
                except Exception:
                    self._slow.setdefault(key, None)
                time.sleep(ttl)
        threading.Thread(target=loop, daemon=True).start()

    def start_background(self):
        # One thread per job, so a slow one (apt, bluetoothctl...) never delays the others.
        self._bg = True
        for key in list(self.slow_jobs):
            self._spawn(key)

    def _sort_top(self):
        if self.sort == "mem":
            key = lambda x: (x[3], x[0])
        else:
            key = lambda x: (x[0], x[3])
        self.top = sorted(self._procs, key=key, reverse=True)[:60]

    # -- fast data, sampled every refresh --
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
        try:
            self.disks = list_disks()
        except Exception:
            self.disks = []
        try:
            per = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            per = {}
        for d in self.disks:
            st = per.get(d["name"])
            if not st:
                continue
            e = self.disk_io.setdefault(d["name"], dict(last=None, r=0.0, w=0.0,
                                                        rh=deque(maxlen=120), wh=deque(maxlen=120)))
            if e["last"]:
                t0, r0, w0 = e["last"]
                dt = max(now - t0, 1e-3)
                e["r"] = max(st.read_bytes - r0, 0) / dt
                e["w"] = max(st.write_bytes - w0, 0) / dt
                e["rh"].append(e["r"])
                e["wh"].append(e["w"])
            e["last"] = (now, st.read_bytes, st.write_bytes)
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
        try:
            b = battery_info()
        except Exception:
            b = None
        if b:
            if b["status"] != self._batt_status:
                self.rate_hist.clear()
                self._batt_status = b["status"]
            if b["rate"]:
                self.rate_hist.append(b["rate"])
        self.battery = b
        self.batt_eta = battery_eta(b, self.rate_hist)
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            i = p.info
            mi = i.get("memory_info")
            procs.append(((i.get("cpu_percent") or 0.0) / self.ncpu, i["pid"],
                          i.get("name") or "?", mi.rss if mi else 0))
        self.ntasks = len(procs)
        self._procs = procs
        self._sort_top()


# --------------------------------------------------------------------------- #
# Tiles. Each returns (lines, level) or (lines, level, title_suffix), where level
# 0/1/2 = ok/warn/crit border, or None to hide the tile entirely (e.g. no battery).
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
    if product and "ASUSLaptop" in product:
        # "Vivobook_ASUSLaptop K5504VA_S5504VA" -> "Vivobook K5504VA S5504VA"
        product = re.sub(r"_?ASUSLaptop\s*", " ", product).replace("_", " ")
        product = re.sub(r"\s+", " ", product).strip()
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
    vs = virt_support()
    in_vm = vs["in_vm"] or bool(re.search(
        r"VirtualBox|VMware|QEMU|KVM|Bochs|Xen|Virtual Machine", f"{vendor_raw} {product}", re.I))
    if in_vm:
        t = txt("VM", "blue")
        v = short_vendor(vendor_raw)
        if v:
            t.append(f"  {v}", style=T["muted"])
        if vs["hw"]:
            t.append(f"  nested {vs['hw']}", style=T["faint"])
        lines.append(kv("Virt", t, w))
    else:
        if vs["hw"]:
            t = txt(vs["hw"], "ok")
            if vs["kvm"]:
                t.append("  KVM ready" if vs["kvm_ok"] else "  KVM (no access)", style=T["muted"])
            else:
                t.append("  no /dev/kvm", style=T["faint"])
        else:
            t = txt("unavailable", "faint")
        lines.append(kv("Virt", t, w))
        lines.append(kv("IOMMU", txt("on", "ok") if vs["iommu"] else txt("off", "faint"), w))
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
                             w, color=T["crit"] if sw.percent >= 90 else T["warn"] if sw.percent >= 70
                             else T["peach"]))
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
    d = c.selected_disk()
    if d is None:
        # No physical disks visible (containers, some VMs): fall back to a plain mount list.
        lines, level = [], 0
        for p in list_mounts()[:4]:
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            lines.append(bar_row(mount_label(p.mountpoint), u.percent,
                                 txt(f"{human_size(u.used)}/{human_size(u.total)}"), w))
            level = max(level, sev(u.percent, 85, 95))
        return (lines or [txt("no disks found", "faint")]), level
    n = len(c.disks)
    idx = [x["name"] for x in c.disks].index(d["name"])
    suffix = d["name"] + (f"  {idx + 1}/{n}" if n > 1 else "")
    lines = [split(txt(d["model"] or d["name"], bold=True), txt(f"{human_size(d['size'])}B", "muted"), w)]
    level = 0
    for p in d["mounts"][:3]:
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        lines.append(bar_row(mount_label(p.mountpoint), u.percent,
                             txt(f"{human_size(u.used)}/{human_size(u.total)}"), w))
        level = max(level, sev(u.percent, 85, 95))
    if len(d["mounts"]) > 3:
        lines.append(txt(f"+{len(d['mounts']) - 3} more", "faint"))
    if not d["mounts"]:
        lines.append(kv("Mount", "not mounted", w, "faint"))
    io = c.disk_io.get(d["name"])
    if io:
        lines.append(rate_row("R", io["rh"], io["r"], "teal", w))
        lines.append(rate_row("W", io["wh"], io["w"], "mauve", w))
    parts = f"{d['nparts']} parts" if d["nparts"] else "whole disk"
    lines.append(kv("Parts", f"{parts} · {len(d['mounts'])} mounted", w))
    row = txt(d["kind"])
    if d["mounts"]:
        row.append(f" · {d['mounts'][0].fstype}")
    if d["usb"]:
        row.append(" · USB", style=T["muted"])
    dt = drive_temp(d["name"])
    if dt is not None:
        row.append(f"  {dt:.0f}°C", style=scolor(dt, 70, 80))
        level = max(level, sev(dt, 70, 80))
    lines.append(kv("Drive", row, w))
    # A nearly full drive you aren't looking at should still light up the border.
    for o in c.disks:
        if o is d:
            continue
        for p in o["mounts"]:
            try:
                if sev(psutil.disk_usage(p.mountpoint).percent, 85, 95):
                    level = max(level, 1)
                    if "!" not in suffix:
                        suffix += "  ! other drive"
            except OSError:
                pass
    return lines, level, suffix


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
    c.pub_row = len(lines)
    if c.pub_reveal:
        pub = c.get("pubip")
        lines.append(kv("Pub. IP", "…" if pub is LOADING else (pub or "–"), w,
                        "bright" if pub not in (None, LOADING) else "faint"))
    else:
        t = Text("▒" * 12, style=T["faint"])
        t.append("  click / p to reveal" if c.mouse_on else "  p to reveal", style=T["faint"])
        lines.append(kv("Pub. IP", t, w))
    ip = "–"
    for a in psutil.net_if_addrs().get(c.iface, []):
        if a.family == socket.AF_INET:
            ip = a.address
    lines.append(kv("Local IP", ip, w))
    dns = c.get("dns")
    if dns not in (None, LOADING) and dns:
        t = txt("  ".join(dns[:2]))
        if len(dns) > 2:
            t.append(f"  +{len(dns) - 2}", style=T["faint"])
        lines.append(kv("DNS", t, w))
    vpn = c.get("vpn")
    if vpn not in (None, LOADING):
        if vpn["active"]:
            t = txt("on", "ok", bold=True)
            t.append("  " + ", ".join(vpn["names"]), style=T["muted"])
            lines.append(kv("VPN", t, w))
        else:
            lines.append(kv("VPN", "off", w, "faint"))
    lines.append(rate_row("↓", c.down_hist, c.down, "blue", w))
    lines.append(rate_row("↑", c.up_hist, c.up, "peach", w))
    if c.net_total:
        lines.append(kv("Total", f"↓ {human_gib(c.net_total[0])}  ↑ {human_gib(c.net_total[1])}", w))
    vendor = c.get("nic")
    if vendor not in (None, LOADING):
        lines.append(kv("Vendor", vendor, w))
    if wifi:
        mon = c.get("monmode")
        if mon is True:
            lines.append(kv("Monitor", "supported", w))
        elif mon is False:
            lines.append(kv("Monitor", "not supported", w, "faint"))
        elif mon is None and not which("iw"):
            lines.append(kv("Monitor", "install iw to check", w, "faint"))
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
    """Scrollable process list. Visible rows grow with the tile; scroll with the wheel or j/k,
    click the header row (or press s) to switch between CPU and memory sorting."""
    la = os.getloadavg()
    lines = [split(txt(f"Load {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}", bold=True),
                   txt(f"{c.ntasks} tasks", "muted"), w)]
    nw = max(w - 21, 6)
    on = f"bold {T['title']}"
    head = Text(f"{'Name':<{nw}} {'PID':>7} ", style=T["muted"])
    head.append(f"{'CPU%':>5}", style=on if c.sort == "cpu" else T["muted"])
    head.append(" ", style=T["muted"])
    head.append(f"{'MEM':>6}", style=on if c.sort == "mem" else T["muted"])
    lines.append(head)
    rows = max(1, min(c.flex.get("Processes", FLEX["Processes"]) - 2, 30))
    n = len(c.top)
    c.proc_off = max(0, min(c.proc_off, max(n - rows, 0)))
    for cpu, pid, name, rss in c.top[c.proc_off:c.proc_off + rows]:
        row = Text(f"{name[:nw]:<{nw}} ", style=T["bright"])
        row.append(f"{pid:>7} ", style=T["muted"])
        row.append(f"{cpu:>5.1f}", style=scolor(cpu, 50, 80))
        row.append(f" {human_size(rss):>6}", style=T["bright"])
        lines.append(row)
    suffix = f"by {c.sort}"
    if n > rows:
        suffix += f"  {c.proc_off + 1}-{min(c.proc_off + rows, n)}/{n}"
    return lines, 0, suffix


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
    rows = max(1, c.flex.get("Peripherals", FLEX["Peripherals"]))
    n = len(items)
    c.periph_off = max(0, min(c.periph_off, max(n - rows, 0)))
    shown = items[c.periph_off:c.periph_off + rows]
    lines = [split(txt(nm), tag, w) for nm, tag in shown]
    suffix = f"{c.periph_off + 1}-{min(c.periph_off + rows, n)}/{n}" if n > rows else ""
    return lines, 0, suffix


def tile_audio(c, w):
    a = c.get("audio")
    if a is LOADING:
        return loading()
    if not a:
        return [txt("no audio server found", "faint")], 0
    muted = a["muted"]
    mic_muted = bool(a.get("mic_muted"))
    lines = [kv("Out", a["out"] or "–", w)]
    if a["vol"] is not None:
        lines.append(bar_row("Vol", min(a["vol"], 100),
                             txt(f"{a['vol']:>3}%", "faint" if muted else "bright"), w,
                             color=T["faint"] if muted else T["blue"]))
    lines.append(kv("In", a["inp"] or "–", w))
    mic = a.get("mic_vol")
    if a.get("mic_monitor"):
        lines.append(kv("Mic", "no mic (monitor source)", w, "faint"))
    elif mic is not None:
        lines.append(bar_row("Mic", min(mic, 100),
                             txt(f"{mic:>3}%", "faint" if mic_muted else "bright"), w,
                             color=T["faint"] if mic_muted else T["mauve"]))
    else:
        lines.append(kv("Mic", "–", w, "faint"))
    if muted or mic_muted:
        t = Text()
        if muted:
            t.append_text(badge("MUTED", "crit"))
        if mic_muted:
            if muted:
                t.append(" ")
            t.append_text(badge("MIC MUTED", "crit"))
        lines.append(kv("Mute", t, w))
    else:
        lines.append(kv("Mute", "off", w, "faint"))
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
    sc = m.get("scale")
    if sc:
        t = txt(f"{sc * 100:.0f}%")
        mm = re.match(r"(\d+)x(\d+)", m["mode"] or "")
        if mm and abs(sc - 1) > 0.01:
            t.append(f"  → {round(int(mm.group(1)) / sc)}×{round(int(mm.group(2)) / sc)}", style=T["faint"])
        lines.append(kv("Scale", t, w))
    bright = brightness()
    if bright is not None:
        lines.append(bar_row("Bright", bright, txt(f"{bright:>3.0f}%"), w, color=T["blue"]))
    lines.append(kv("Screens", str(len(mons)), w))
    lines.append(kv("Session", (os.environ.get("XDG_SESSION_TYPE") or "–").capitalize(), w))
    for extra in mons[1:3]:
        lines.append(kv(extra["name"][:8], f"{(extra['mode'] or '–').replace('x', '×')}", w))
    return lines, 0


def tile_battery(c, w):
    b = c.battery
    if not b:
        return None
    pct, status = b["pct"], b["status"]
    col = T["ok"] if pct > 30 else T["warn"] if pct > 15 else T["crit"]
    head = txt(f"{pct:>3.0f}% ", col, bold=True)
    head.append_text(bar(pct, max(w - head.cell_len, 4), col))
    symbol = {"Charging": "▲ Charging", "Discharging": "▼ Discharging", "Full": "● Full",
              "Not charging": "– Not charging"}.get(status, status)
    lines = [head, kv("Status", txt(symbol, "ok" if status == "Charging" else "bright"), w)]
    if status in ("Charging", "Discharging"):
        label = "To full" if status == "Charging" else "Left"
        if c.batt_eta:
            h, m = divmod(int(c.batt_eta) // 60, 60)
            lines.append(kv(label, f"{h}h {m:02d}m", w))
        else:
            lines.append(kv(label, "–", w, "faint"))
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


def footer(interval, theme, interactive, multi_disk, mouse):
    t = Table(show_header=False, show_edge=False, box=None, expand=True,
              padding=(0, 1), style=f"on {T['bar']}")
    t.add_column(ratio=4, justify="left", no_wrap=True, overflow="ellipsis")
    t.add_column(ratio=1, justify="right", no_wrap=True)
    key = f"bold {T['peach']}"
    hint = Text.assemble(("Ctrl+C", key), (" quit", T["muted"]))
    if interactive:
        hint.append("   s", style=key)
        hint.append(" sort", style=T["muted"])
        hint.append("   p", style=key)
        hint.append(" ip", style=T["muted"])
        hint.append("   wheel" if mouse else "   j/k", style=key)
        hint.append(" scroll", style=T["muted"])
        if mouse:
            hint.append("   click", style=key)
            hint.append(" focus", style=T["muted"])
        if multi_disk:
            hint.append("   [ ]", style=key)
            hint.append(" drive", style=T["muted"])
    t.add_row(hint, txt(f"every {interval:g}s · {theme}", "faint"))
    return t


def make_panel(tile, width, height, limit, focused=False):
    name, lines, level, suffix = tile
    lines = lines[:limit] if limit else lines
    for ln in lines:
        ln.no_wrap = True
        ln.overflow = "ellipsis"
    border = T["blue"] if focused else (T["border"], T["warn"], T["crit"])[level]
    title = Text(name, style=f"bold {T['title']}")
    if suffix:
        title.append("  " + suffix, style=T["muted"])
    return Panel(Group(*lines), title=title, title_align="left",
                 border_style=border, box=box.ROUNDED, padding=(0, 1), width=width, height=height)


def tile_len(t):
    """Natural height of a tile's content. Scrollable tiles are capped so that showing more rows
    when space is available never makes the row itself grow."""
    n = len(t[1])
    return min(n, FLEX[t[0]]) if t[0] in FLEX else n


def render(c, console, interval, theme, fill, interactive=False, mouse=False, light=False):
    """light=True reuses the last output of tiles that don't react to input (used while
    scrolling), so only the scrollable tiles are recomputed."""
    width, height = console.size
    cols = 4 if width >= 128 else 3 if width >= 96 else 2 if width >= 64 else 1
    base, extra = divmod(width - (cols - 1), cols)
    widths = [base + (1 if i < extra else 0) for i in range(cols)]
    tiles = []
    for name, fn in TILES:
        w = widths[len(tiles) % cols] - 4
        cached = c.tile_cache.get(name)
        if light and cached and cached[0] == w and name not in FLEX and name != "Storage":
            res = cached[1]
        else:
            try:
                res = fn(c, w)
            except Exception:
                res = ([txt("unavailable", "faint")], 0)
            c.tile_cache[name] = (w, res)
        if res is not None:
            tiles.append((name, res[0], res[1], res[2] if len(res) > 2 else ""))
    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    avail = height - 2
    limit = None
    for lim in (None, 8, 6, 5, 4, 3):
        limit = lim
        need = sum(min(max(tile_len(t) for t in r), lim or 99) + 2 for r in rows)
        if need <= avail:
            break
    heights = [min(max(tile_len(t) for t in r), limit or 99) + 2 for r in rows]
    if fill:
        spare = min(max(avail - sum(heights), 0) // len(rows), 3)
        heights = [h + spare for h in heights]

    # Remember where every tile sits (for mouse hit-testing) and how many lines the
    # scrollable tiles may use on the next frame.
    c.hitboxes = []
    y = 1                                   # row 0 is the header bar
    for r, h in zip(rows, heights):
        for i, t in enumerate(r):
            x0 = sum(widths[:i]) + i
            c.hitboxes.append((t[0], x0, y, x0 + widths[i], y + h))
            if t[0] in FLEX:
                c.flex[t[0]] = h - 2
        y += h

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
            cells.append(make_panel(r[i], widths[i], h, limit, interactive and c.focus == r[i][0])
                         if i < len(r) else "")
        grid.add_row(*cells)
    return Group(header(), grid,
                 footer(interval, theme, interactive, interactive and len(c.disks) > 1, mouse))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(prog="sysinfo", description="Tile-based system info dashboard.")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--theme", choices=list(THEMES), default="mocha")
    ap.add_argument("--disk", metavar="NAME",
                    help="start the Storage tile on this drive, e.g. nvme0n1 (switch live with [ and ])")
    ap.add_argument("--public-ip", action="store_true",
                    help="look up public IP via api.ipify.org at start and show it "
                         "(default: hidden, looked up only when you reveal it)")
    ap.add_argument("--no-mouse", action="store_true",
                    help="don't capture the mouse (normal text selection; scroll with j/k)")
    args = ap.parse_args()
    args.interval = max(args.interval, 0.5)
    T.clear()
    T.update(THEMES[args.theme])

    console = Console()
    c = Collector(public_ip=args.public_ip)
    c.disk_sel = args.disk

    if args.once or not sys.stdout.isatty():
        c.sample()
        time.sleep(0.5)
        c.sample()
        time.sleep(0.5)
        c.sample()
        c.refresh_slow(force=True)
        console.print(render(c, console, args.interval, args.theme, fill=False))
        return 0

    c.sample()              # first sample sets the active interface and drives the slow jobs use
    c.start_background()
    time.sleep(0.4)
    try:
        with Keys(mouse=not args.no_mouse) as keys, \
                Live(console=console, screen=True, auto_refresh=False,
                     vertical_overflow="crop") as live:
            c.mouse_on = keys.mouse

            def draw(light=False):
                live.update(render(c, console, args.interval, args.theme, True,
                                   keys.active, keys.mouse, light), refresh=True)

            while True:
                c.sample()
                draw()
                deadline = time.monotonic() + args.interval
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    events = keys.wait(left)
                    while events:                    # drain anything already queued
                        more = keys.wait(0)
                        if not more:
                            break
                        events += more
                    for kind, val, x, y in events:
                        if kind == "quit":
                            return 0
                        c.handle(kind, val, x, y)
                    if events:
                        draw(light=True)             # one redraw for the whole burst
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())