"""UDP path health via STUN binding requests (games use UDP), plus a basic NAT type check."""
import os
import socket
import struct
import time

from .latency import summarize

MAGIC = 0x2112A442


def _request():
    tid = os.urandom(12)
    return struct.pack("!HHI", 0x0001, 0, MAGIC) + tid, tid


def parse_mapped_address(data, tid):
    """Return (ip, port) from a STUN binding success response, or None."""
    if len(data) < 20:
        return None
    msg_type, length, cookie = struct.unpack("!HHI", data[:8])
    if msg_type != 0x0101 or data[8:20] != tid:
        return None
    pos, end = 20, min(len(data), 20 + length)
    fallback = None
    while pos + 4 <= end:
        atype, alen = struct.unpack("!HH", data[pos:pos + 4])
        val = data[pos + 4:pos + 4 + alen]
        if atype in (0x0020, 0x0001) and len(val) >= 8 and val[1] == 0x01:
            port = struct.unpack("!H", val[2:4])[0]
            ip = bytes(val[4:8])
            if atype == 0x0020:
                port ^= MAGIC >> 16
                ip = bytes(b ^ m for b, m in zip(ip, struct.pack("!I", MAGIC)))
                return socket.inet_ntoa(ip), port
            fallback = (socket.inet_ntoa(ip), port)
        pos += 4 + alen + ((4 - alen % 4) % 4)
    return fallback


def _transact(sock, addr, timeout):
    req, tid = _request()
    sock.settimeout(timeout)
    start = time.perf_counter()
    try:
        sock.sendto(req, addr)
        deadline = start + timeout
        while True:
            data, _ = sock.recvfrom(2048)
            mapped = parse_mapped_address(data, tid)
            if mapped:
                return (time.perf_counter() - start) * 1000.0, mapped
            if time.perf_counter() > deadline:
                return None, None
    except OSError:
        return None, None


def stun_test(servers, count=40, interval=0.2, timeout=1.0, on_sample=None):
    """Probe each STUN server from ONE local socket so mapped ports can be compared for NAT type.
    on_sample(rtt) is called per packet (None = no reply) so callers can show results live."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    addrs = []
    for host, port in servers:
        try:
            addrs.append((socket.gethostbyname(host), port))
        except OSError:
            pass
    samples, mappings = [], {}
    try:
        if not addrs:
            return {"ok": False, "error": "STUN servers could not be resolved", **summarize([])}
        for i in range(count):
            addr = addrs[i % len(addrs)]
            t0 = time.perf_counter()
            rtt, mapped = _transact(sock, addr, timeout)
            samples.append(rtt)
            if on_sample is not None:
                on_sample(rtt)
            if mapped:
                mappings.setdefault(addr, mapped)
            time.sleep(max(0.0, interval - (time.perf_counter() - t0)))
    finally:
        sock.close()

    out = summarize(samples)
    out["ok"] = out["received"] > 0
    out["public_ip"] = next(iter(mappings.values()))[0] if mappings else None
    ports = {m[1] for m in mappings.values()}
    if len(mappings) < 2:
        out["nat"] = "Unknown"
    elif len(ports) == 1:
        out["nat"] = "Open/Moderate"  # endpoint-independent mapping
    else:
        out["nat"] = "Strict"  # symmetric NAT: mapping changes per destination
    return out
