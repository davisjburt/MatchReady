"""Download/upload throughput against Cloudflare's speed test endpoints, with latency under load."""
import threading
import time
import urllib.request

from .latency import ping_series, summarize

DOWN_URL = "https://speed.cloudflare.com/__down?bytes=50000000"
UP_URL = "https://speed.cloudflare.com/__up"
HEADERS = {"User-Agent": "MatchReady/1.0"}
WARMUP = 1.0


class _Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.at_warmup = None

    def add(self, n):
        with self.lock:
            self.total += n


def _download_worker(counter, stop):
    while not stop.is_set():
        try:
            req = urllib.request.Request(DOWN_URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=5) as resp:
                while not stop.is_set():
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    counter.add(len(chunk))
        except OSError:
            time.sleep(0.2)


class _UploadBody:
    """File-like body that counts bytes as http.client sends them, and aborts when stopped."""
    SIZE = 8_000_000
    BLOCK = b"\0" * 65536

    def __init__(self, counter, stop):
        self.counter, self.stop, self.sent = counter, stop, 0

    def read(self, n=-1):
        if self.stop.is_set():
            raise OSError("upload stopped")
        n = min(n if n and n > 0 else len(self.BLOCK), len(self.BLOCK), self.SIZE - self.sent)
        if n <= 0:
            return b""
        self.sent += n
        self.counter.add(n)
        return self.BLOCK[:n]


def _upload_worker(counter, stop):
    while not stop.is_set():
        try:
            body = _UploadBody(counter, stop)
            req = urllib.request.Request(UP_URL, data=body, method="POST", headers={
                **HEADERS, "Content-Type": "application/octet-stream",
                "Content-Length": str(_UploadBody.SIZE)})
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except OSError:
            time.sleep(0.1)


def _run_load(worker, streams, duration, loaded_pinger, on_sample=None):
    """Run a load phase; returns (mbps, loaded latency summary), or (None, None) if no data moved.
    on_sample(rtt, mbps_so_far) is called during the measured window (rtt None = lost / no pinger)."""
    counter, stop = _Counter(), threading.Event()
    threads = [threading.Thread(target=worker, args=(counter, stop), daemon=True) for _ in range(streams)]
    for t in threads:
        t.start()
    time.sleep(WARMUP)  # let TCP ramp up before measuring
    with counter.lock:
        start_bytes = counter.total
    start = time.perf_counter()

    def mbps_so_far():
        with counter.lock:
            moved = counter.total - start_bytes
        el = time.perf_counter() - start
        return (moved * 8 / 1e6) / el if el > 0 else 0.0

    try:  # the streams must stop even if on_sample raises (e.g. the check was cancelled)
        if loaded_pinger:
            tick = (lambda rtt: on_sample(rtt, mbps_so_far())) if on_sample else None
            samples = ping_series(loaded_pinger, duration=duration - WARMUP, interval=0.1, on_sample=tick)
        else:
            samples = []
            end = start + duration - WARMUP
            while time.perf_counter() < end:
                time.sleep(0.2)
                if on_sample:
                    on_sample(None, mbps_so_far())
        elapsed = time.perf_counter() - start
        with counter.lock:
            moved = counter.total - start_bytes
    finally:
        stop.set()
    if moved <= 0:
        # Nothing flowed (endpoint blocked, rate-limited or unreachable): the test failed, which says
        # nothing about the connection, and the "loaded" pings were not loaded.
        return None, None
    mbps = (moved * 8 / 1e6) / elapsed if elapsed > 0 else 0.0
    return mbps, summarize(samples) if samples else None


def download_test(loaded_pinger, duration=7.0, streams=4, on_sample=None):
    return _run_load(_download_worker, streams, duration, loaded_pinger, on_sample)


def upload_test(loaded_pinger, duration=5.0, streams=3, on_sample=None):
    return _run_load(_upload_worker, streams, duration, loaded_pinger, on_sample)
