"""Latency / jitter / packet-loss probes (ICMP with TCP-connect fallback)."""
import itertools
import os
import socket
import statistics
import threading
import time

from icmplib import ICMPv4Socket, ICMPRequest
from icmplib.exceptions import ICMPLibError

_id_counter = itertools.count((os.getpid() & 0x7FFF) + 1)
_id_lock = threading.Lock()


def _next_id():
    with _id_lock:
        return next(_id_counter) & 0xFFFF


def resolve(host):
    return socket.gethostbyname(host)


class Pinger:
    """Sends one probe at a time to a host; returns RTT in ms or None on loss."""

    def __init__(self, ip, method="icmp", tcp_port=None, timeout=1.0):
        self.ip, self.method, self.tcp_port, self.timeout = ip, method, tcp_port, timeout
        self._id = _next_id()
        self._seq = 0
        self._sock = ICMPv4Socket(privileged=False) if method == "icmp" else None

    def probe(self):
        if self.method == "icmp":
            self._seq = (self._seq + 1) & 0xFFFF
            req = ICMPRequest(destination=self.ip, id=self._id, sequence=self._seq)
            try:
                self._sock.send(req)
                reply = self._sock.receive(req, self.timeout)
                reply.raise_for_status()
                return (reply.time - req.time) * 1000.0
            except (ICMPLibError, OSError):
                return None
        start = time.perf_counter()
        try:
            with socket.create_connection((self.ip, self.tcp_port), timeout=self.timeout):
                return (time.perf_counter() - start) * 1000.0
        except OSError:
            return None

    def close(self):
        if self._sock:
            self._sock.close()


def open_pinger(host, tcp_port=None, timeout=1.0):
    """Resolve host and pick ICMP if it answers, else TCP connect. Returns Pinger or None."""
    try:
        ip = resolve(host)
    except OSError:
        return None
    try:
        p = Pinger(ip, "icmp", timeout=timeout)
        if any(p.probe() is not None for _ in range(3)):
            return p
        p.close()
    except (ICMPLibError, OSError):
        pass
    if tcp_port:
        p = Pinger(ip, "tcp", tcp_port, timeout=timeout)
        if any(p.probe() is not None for _ in range(2)):
            return p
    return None


def ping_series(pinger, duration=None, count=None, interval=0.15, stop_event=None, on_sample=None):
    """Probe repeatedly; returns list of RTTs (None = lost) in send order.
    on_sample(rtt) is called as each probe completes, so callers can show results live."""
    samples = []
    end = time.perf_counter() + duration if duration else None
    while True:
        if count is not None and len(samples) >= count:
            break
        if end is not None and time.perf_counter() >= end:
            break
        if stop_event is not None and stop_event.is_set():
            break
        t0 = time.perf_counter()
        samples.append(pinger.probe())
        if on_sample is not None:
            on_sample(samples[-1])
        remaining = interval - (time.perf_counter() - t0)
        if remaining > 0:
            time.sleep(remaining)
    return samples


def summarize(samples):
    """Stats used by scoring. Jitter = mean |consecutive RTT difference| (RFC 3550 style)."""
    n = len(samples)
    got = [s for s in samples if s is not None]
    out = {"sent": n, "received": len(got)}
    out["loss_pct"] = 100.0 * (n - len(got)) / n if n else 100.0
    burst = cur = 0
    for s in samples:
        cur = cur + 1 if s is None else 0
        burst = max(burst, cur)
    out["max_loss_burst"] = burst
    if not got:
        out.update(avg=None, min=None, median=None, p95=None, jitter=None, spike_pct=None)
        return out
    srt = sorted(got)
    med = statistics.median(got)
    out["avg"] = statistics.fmean(got)
    out["min"] = srt[0]
    out["median"] = med
    out["p95"] = srt[min(len(srt) - 1, int(round(0.95 * (len(srt) - 1))))]
    diffs = [abs(b - a) for a, b in zip(got, got[1:])]
    out["jitter"] = statistics.fmean(diffs) if diffs else 0.0
    # A "lag spike" is a reply more than 40 ms above the typical RTT.
    out["spike_pct"] = 100.0 * sum(1 for s in got if s > med + 40) / len(got)
    return out
