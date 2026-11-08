"""Local factors: connection type, gateway, DNS, captive portal, VPN, CPU, background traffic."""
import re
import socket
import subprocess
import time
import urllib.request
import uuid

import psutil

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VPN_WORDS = ("vpn", "tap-", "tap ", "wireguard", "wintun", "openvpn", "nordlynx", "proton",
             "mullvad", "expressvpn", "surfshark", "cisco anyconnect", "globalprotect", "forti")
# Apps that commonly saturate a connection in the background.
HEAVY_APPS = {
    "onedrive.exe": "OneDrive", "dropbox.exe": "Dropbox", "googledrivefs.exe": "Google Drive",
    "steam.exe": "Steam", "epicgameslauncher.exe": "Epic Games Launcher",
    "battle.net.exe": "Battle.net", "qbittorrent.exe": "qBittorrent", "utorrent.exe": "uTorrent",
    "bittorrent.exe": "BitTorrent", "eadesktop.exe": "EA app", "upc.exe": "Ubisoft Connect",
    "obs64.exe": "OBS", "icloudservices.exe": "iCloud", "creative cloud.exe": "Creative Cloud",
}


def default_gateway():
    try:
        out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True,
                             timeout=5, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    best = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            if re.match(r"\d+\.\d+\.\d+\.\d+$", parts[2]):
                metric = int(parts[4]) if parts[4].isdigit() else 9999
                if best is None or metric < best[1]:
                    best = (parts[2], metric)
    return best[0] if best else None


def active_interface():
    """Name of the interface carrying internet traffic, and whether it is wired or Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        local_ip = s.getsockname()[0]
        s.close()
    except OSError:
        return None, "unknown"
    for name, addrs in psutil.net_if_addrs().items():
        if any(a.address == local_ip for a in addrs):
            low = name.lower()
            kind = "wifi" if any(w in low for w in ("wi-fi", "wifi", "wlan", "wireless")) else "wired"
            return name, kind
    return None, "unknown"


def vpn_adapters():
    up = psutil.net_if_stats()
    return [n for n, st in up.items() if st.isup and any(w in n.lower() for w in VPN_WORDS)]


def heavy_apps_running():
    found = set()
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        if name in HEAVY_APPS:
            found.add(HEAVY_APPS[name])
    return sorted(found)


def dns_time_ms(samples=3):
    """Time to resolve uncached names (random subdomains force a full lookup)."""
    times = []
    for _ in range(samples):
        host = f"mr-{uuid.uuid4().hex[:10]}.cloudflare.com"
        t = time.perf_counter()
        try:
            socket.getaddrinfo(host, 443)
        except OSError:
            pass  # NXDOMAIN still measures resolver round trip
        times.append((time.perf_counter() - t) * 1000.0)
    return sorted(times)[len(times) // 2]


def captive_portal():
    """True if a login page intercepts traffic (e.g. campus Wi-Fi sign-in expired)."""
    try:
        with urllib.request.urlopen("http://www.msftconnecttest.com/connecttest.txt", timeout=4) as r:
            return r.read(64).strip() != b"Microsoft Connect Test"
    except OSError:
        return None


class TrafficSampler:
    """Measures total traffic on the active NIC while our own (tiny) ping traffic runs."""

    def __init__(self, iface):
        self.iface = iface
        self.start = self._read()
        self.t0 = time.perf_counter()

    def _read(self):
        c = psutil.net_io_counters(pernic=True).get(self.iface) if self.iface else None
        return (c.bytes_recv, c.bytes_sent) if c else None

    def mbps(self):
        end = self._read()
        if not self.start or not end:
            return None, None
        dt = time.perf_counter() - self.t0
        return ((end[0] - self.start[0]) * 8 / 1e6 / dt, (end[1] - self.start[1]) * 8 / 1e6 / dt)


def cpu_percent():
    return psutil.cpu_percent(interval=1.0)
