"""Wi-Fi link quality from `netsh wlan` (no admin needed)."""
import re
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _netsh(*args):
    try:
        return subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True,
                              timeout=8, creationflags=NO_WINDOW, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _num(s):
    m = re.search(r"-?\d+(\.\d+)?", s or "")
    return float(m.group()) if m else None


def parse_interfaces(text):
    """Parse `netsh wlan show interfaces` into a dict for the first connected interface."""
    blocks, cur = [], {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "name":
            if cur:
                blocks.append(cur)
            cur = {}
        elif not cur:
            continue  # header text before the first interface
        cur.setdefault(key, val)
    if cur:
        blocks.append(cur)
    conn = next((b for b in blocks if b.get("state", "").lower() == "connected"), None)
    if not conn:
        return {"connected": False, "adapter": blocks[0].get("description") if blocks else None}
    channel = _num(conn.get("channel"))
    band = conn.get("band")
    if not band and channel:
        band = "2.4 GHz" if channel <= 14 else "5 GHz"
    signal = _num(conn.get("signal"))
    rssi = _num(conn.get("rssi"))
    if rssi is None and signal is not None:
        rssi = signal / 2 - 100  # Windows' own mapping of quality % to dBm
    return {
        "connected": True,
        "interface": conn.get("name"),
        "adapter": conn.get("description"),
        "ssid": conn.get("ssid"),
        "bssid": conn.get("ap bssid") or conn.get("bssid"),
        "radio": conn.get("radio type"),
        "band": band,
        "channel": int(channel) if channel else None,
        "signal_pct": signal,
        "rssi_dbm": rssi,
        "rx_mbps": _num(conn.get("receive rate (mbps)")),
        "tx_mbps": _num(conn.get("transmit rate (mbps)")),
    }


def parse_networks(text):
    """Count visible access points (BSSIDs) per channel from `netsh wlan show networks mode=bssid`."""
    per_channel = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        if key.strip().lower() == "channel":
            ch = _num(val)
            if ch:
                per_channel[int(ch)] = per_channel.get(int(ch), 0) + 1
    return per_channel


def wifi_info():
    info = parse_interfaces(_netsh("show", "interfaces"))
    if info.get("connected"):
        per_channel = parse_networks(_netsh("show", "networks", "mode=bssid"))
        ch = info.get("channel")
        # Count other APs on our channel (minus our own) and on overlapping 2.4 GHz channels.
        same = max(0, per_channel.get(ch, 0) - 1) if per_channel else None
        if same is not None and ch and ch <= 14:
            same += sum(n for c, n in per_channel.items() if c != ch and c <= 14 and abs(c - ch) < 5)
        info["aps_same_channel"] = same
        info["aps_visible"] = sum(per_channel.values()) if per_channel else None
    return info
