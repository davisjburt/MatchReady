"""Coordinates the probes into a ~30 second check and assembles the metrics dict scoring expects.

While a check runs, a LiveRun collects samples as they arrive so the UI can show what is happening
and a predicted score long before the final result.
"""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import scoring
from .probes import bandwidth, system, wifi
from .probes.latency import open_pinger, ping_series, summarize
from .probes.udp_stun import stun_test

IDLE_SECONDS = 14.0
DOWN_SECONDS = 7.0
UP_SECONDS = 5.0

MIN_LIVE_PINGS = 6        # replies needed before there is a predicted score
LIVE_LOSS_FLOOR = 60      # early loss is judged as if at least this many pings were sent (1 early drop != 2% loss)
LIVE_UDP_FLOOR = 20
TRACE_POINTS = 100        # points kept for the live ping graph

STEPS = [("net", "Network"), ("ping", "Game servers"), ("router", "Your router"),
         ("udp", "Game traffic (UDP)"), ("down", "Download + lag under load"),
         ("up", "Upload + lag under load"), ("sys", "Wi-Fi and PC")]


def load_config():
    """targets.json next to the exe (user-editable) wins over the bundled copy."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "targets.json")
        candidates.append(Path(getattr(sys, "_MEIPASS", "")) / "matchready" / "targets.json")
    candidates.append(Path(__file__).with_name("targets.json"))
    for p in candidates:
        if p.is_file():
            return normalize_config(json.loads(p.read_text(encoding="utf-8")))
    raise FileNotFoundError("targets.json not found")


def normalize_config(cfg):
    """Older targets.json files listed servers per game; merge those into one pool (deduped by host)."""
    if "servers" not in cfg:
        seen, servers = set(), []
        for g in (cfg.get("games") or {}).values():
            for t in g.get("targets", []):
                if t["host"] not in seen:
                    seen.add(t["host"])
                    servers.append(t)
        cfg = {**cfg, "servers": servers}
    cfg.setdefault("region", "US Central")
    return cfg


def _series(host, tcp_port, duration=None, count=None, interval=0.15, live=None, key=None):
    p = open_pinger(host, tcp_port)
    if p is None:
        return {"method": None, **summarize([])}
    if live:
        live.set_method(key, p.method)
    try:
        samples = ping_series(p, duration=duration, count=count, interval=interval,
                              on_sample=live.sink(key) if live else None)
        return {"method": p.method, **summarize(samples)}
    finally:
        p.close()


def _aggregate(servers, target_stats, min_sent=0):
    targets = [{"name": t["name"], "host": t["host"], **s} for t, s in zip(servers, target_stats)]
    reachable = [t for t in targets if t["received"]]
    g = {"targets": targets}
    if not reachable:
        return {**g, "latency": None, "jitter": None, "spike_pct": None, "loss_pct": 100.0,
                "max_loss_burst": 0, "primary": None}
    # Matchmaking puts you on the best nearby datacenter, so the lowest-latency one represents the path a game
    # would use. Loss is pooled across all of them because local loss hits every path.
    best = min(reachable, key=lambda t: t["avg"])
    sent = sum(t["sent"] for t in reachable)
    recv = sum(t["received"] for t in reachable)
    return {**g, "primary": best["name"], "latency": best["avg"], "p95": best["p95"], "jitter": best["jitter"],
            "spike_pct": best["spike_pct"], "loss_pct": 100.0 * (sent - recv) / max(sent, min_sent),
            "max_loss_burst": max(t["max_loss_burst"] for t in reachable),
            "method": best["method"]}


def network_label(m):
    conn = m.get("connection", {})
    w = m.get("wifi") or {}
    if conn.get("type") == "wifi" and w.get("ssid"):
        return f"Wi-Fi: {w['ssid']}"
    return conn.get("interface") or "Unknown"


def score_all(m):
    return scoring.score(m)


class Cancelled(Exception):
    """Raised inside a check when its cancel event is set; probes stop at their next sample."""


def _then(future, fn):
    """Call fn(result) when a future finishes (errors are ignored; the main flow reports them)."""
    def cb(f):
        try:
            fn(f.result())
        except Exception:
            pass
    future.add_done_callback(cb)


def _on_all_done(futures, fn):
    left, lock = [len(futures)], threading.Lock()

    def cb(_):
        with lock:
            left[0] -= 1
            if left[0] == 0:
                fn()
    for f in futures:
        f.add_done_callback(cb)


def _ms(v, digits=0):
    return "—" if v is None else f"{v:.{digits}f} ms"


class LiveRun:
    """Thread-safe record of a check in progress. Probes feed it samples as they arrive; snapshot() turns
    whatever exists so far into partial metrics, a predicted score, activity rows and graph traces."""

    def __init__(self, cfg, prior=None, cancel=None):
        self.cfg, self.prior = cfg, prior or {}
        self.cancel = cancel
        self.lock = threading.Lock()
        self.rtts, self.methods = {}, {}
        self.udp_rtts = []
        self.loaded = {"down": [], "up": []}
        self.mbps = {"down": None, "up": None}
        self.steps = {sid: "wait" for sid, _ in STEPS}
        self.info = {"system": {}}
        self.ping_started = None
        self.traffic = None

    # ---- feeding (called from probe threads)
    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def sink(self, key):
        def add(rtt):
            self.check()
            with self.lock:
                self.rtts.setdefault(key, []).append(rtt)
        return add

    def set_method(self, key, method):
        with self.lock:
            self.methods[key] = method

    def step(self, sid, state):
        with self.lock:
            self.steps[sid] = state

    def put(self, **kw):
        with self.lock:
            self.info.update(kw)

    def put_system(self, **kw):
        with self.lock:
            self.info["system"].update(kw)

    def add_udp(self, rtt):
        self.check()
        with self.lock:
            self.udp_rtts.append(rtt)

    def add_loaded(self, phase, rtt, mbps):
        self.check()
        with self.lock:
            self.loaded[phase].append(rtt)
            self.mbps[phase] = mbps

    def poll_traffic(self):
        """Background traffic on the NIC, measured only while we are not generating load ourselves."""
        with self.lock:
            idle = self.steps["down"] == "wait" and self.ping_started is not None
            traffic, started = self.traffic, self.ping_started
        if traffic and idle and time.perf_counter() - started >= 4:
            down, up = traffic.mbps()
            self.put_system(bg_down_mbps=down, bg_up_mbps=up)

    # ---- reading
    def snapshot(self):
        with self.lock:
            rtts = {k: list(v) for k, v in self.rtts.items()}
            methods = dict(self.methods)
            udp = list(self.udp_rtts)
            loaded = {k: list(v) for k, v in self.loaded.items()}
            mbps = dict(self.mbps)
            steps = dict(self.steps)
            info = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.info.items()}
            ping_started = self.ping_started

        conn = dict(info.get("connection") or {})
        if info.get("gateway_ip"):
            conn["gateway"] = info["gateway_ip"]
        # last run's UDP/bufferbloat/speed only stand in while on the same interface
        same_net = conn.get("interface") and (self.prior.get("connection") or {}).get("interface") == conn["interface"]
        prior = self.prior if same_net else {}

        m = {"connection": conn, "captive_portal": bool(info.get("captive_portal")),
             "wifi": info.get("wifi") or {}, "system": info.get("system") or {}}

        servers = self.cfg["servers"]
        stats = [{"method": methods.get(("srv", i)), **summarize(rtts.get(("srv", i), []))}
                 for i in range(len(servers))]
        pings = sum(s["received"] for s in stats)
        ready = pings >= MIN_LIVE_PINGS
        if ready:
            m["servers"] = _aggregate(servers, stats, LIVE_LOSS_FLOOR)

        gw = rtts.get("gw", [])
        if len(gw) >= 3 and any(v is not None for v in gw):
            m["gateway"] = {"method": methods.get("gw"), **summarize(gw)}

        from_prior = set()  # values borrowed from the last check: they feed the prediction, not the tiles
        udp_measured = bool(info.get("udp"))
        if udp_measured:
            m["udp"] = info["udp"]
        elif len(udp) >= 6:
            s = summarize(udp)
            s["loss_pct"] = 100.0 * (s["sent"] - s["received"]) / max(s["sent"], LIVE_UDP_FLOOR)
            s["ok"] = s["received"] > 0
            m["udp"], udp_measured = s, True
        elif prior.get("udp"):
            m["udp"] = prior["udp"]
            from_prior.add("udp")

        ref = summarize(rtts.get("ref", []))
        idle = ref["avg"] if ref["received"] >= 5 else None
        bloat, lloss = [], []
        for ph in ("down", "up"):
            vals = loaded[ph]
            got = [v for v in vals if v is not None]
            if idle is not None and len(got) >= 3:
                bloat.append(sum(got) / len(got) - idle)
                lloss.append(100.0 * (len(vals) - len(got)) / len(vals))
        bloat_measured = bool(bloat)
        if bloat:
            m["bufferbloat_ms"], m["loaded_loss_pct"] = max(bloat), max(lloss)
        elif prior.get("bufferbloat_ms") is not None:
            m["bufferbloat_ms"], m["loaded_loss_pct"] = prior["bufferbloat_ms"], prior.get("loaded_loss_pct") or 0.0
            from_prior |= {"bufferbloat_ms", "loaded_loss_pct"}
        for key in ("download_mbps", "upload_mbps"):
            val = info.get(key, prior.get(key))
            if val is not None:
                m[key] = val
                if key not in info:
                    from_prior.add(key)

        if info.get("ping_done"):
            ping_frac = 1.0
        elif ping_started is not None:
            ping_frac = min(1.0, (time.perf_counter() - ping_started) / IDLE_SECONDS)
        else:
            ping_frac = 0.0
        estimate = None
        if ready:
            res = scoring.score(m)
            spread = 0 if m["captive_portal"] else round(min(25.0, 3 + 13 * (1 - ping_frac)
                                                          + (0 if udp_measured else (3 if prior.get("udp") else 5))
                                                          + (0 if bloat_measured else (4 if prior else 7))))
            estimate = {"score": res["score"], "verdict": res["verdict"], "caps": res["caps"],
                        "feel": res["feel"], "spread": spread, "pings": pings,
                        "low": max(0, res["score"] - spread), "high": min(100, res["score"] + spread)}

        title, traces, counts = self._traces(steps, m, rtts, loaded)
        return {"m": m, "view": {k: v for k, v in m.items() if k not in from_prior},
                "estimate": estimate, "traces": traces, "trace_title": title, "trace_counts": counts,
                "activity": self._activity(steps, m, info, rtts, udp, loaded, mbps, bloat, pings)}

    def _traces(self, steps, m, rtts, loaded):
        """-> (title, {key: last TRACE_POINTS values}, {key: total values so far}). The totals let the graph
        scroll smoothly once a series is longer than the window."""
        if steps["down"] == "run":
            full, title = {"load": loaded["down"]}, "Ping while downloading"
        elif steps["up"] == "run":
            full, title = {"load": loaded["up"]}, "Ping while uploading"
        else:
            full, title = {}, "Ping to the game servers"
            g = m.get("servers")
            if g and g.get("primary"):
                names = [t["name"] for t in self.cfg["servers"]]
                full["servers"] = rtts.get(("srv", names.index(g["primary"])), [])
            if rtts.get("gw"):
                full["router"] = rtts["gw"]
        n = TRACE_POINTS
        return title, {k: v[-n:] for k, v in full.items()}, {k: len(v) for k, v in full.items()}

    def _activity(self, steps, m, info, rtts, udp, loaded, mbps, bloat, pings):
        conn, w, sysm = m["connection"], m["wifi"], m["system"]
        d = {}
        kind = {"wired": "Ethernet", "wifi": "Wi-Fi"}.get(conn.get("type"), "Network")
        d["net"] = (f"{kind} ({conn['interface']})" if conn.get("interface") else "Checking…") + \
                   (f" · router {conn['gateway']}" if conn.get("gateway") else "")
        g = m.get("servers")
        lat = f"best {_ms(g['latency'])}" if g and g.get("latency") is not None else ""
        sent = sum(len(v) for k, v in rtts.items() if isinstance(k, tuple))
        lost = sum(1 for k, v in rtts.items() if isinstance(k, tuple) for x in v if x is None)
        d["ping"] = f"{sent} pings, {lost} lost" + (f" · {lat}" if lat else "")
        gwm = m.get("gateway")
        d["router"] = (f"{_ms(gwm['avg'], 1)} · {_ms(gwm['jitter'], 1)} jitter" if gwm and gwm.get("avg") is not None
                       else "Looking for your router…" if steps["router"] != "skip" else "No router found")
        u = m.get("udp")
        if u and (len(udp) >= 6 or info.get("udp")):
            d["udp"] = ("Blocked: no replies" if not u.get("ok") else
                        f"{u['received']}/{u['sent']} replies · {_ms(u.get('median'))}"
                        + (f" · NAT {u['nat']}" if u.get("nat") else ""))
        else:
            d["udp"] = f"{len(udp)} packets sent…" if udp else ""
        for ph, key in (("down", "download_mbps"), ("up", "upload_mbps")):
            vals = [v for v in loaded[ph] if v is not None]
            if steps[ph] == "done":
                d[ph] = (f"{info[key]:.0f} Mbps" if info.get(key) is not None
                         else "Unavailable (speed test blocked or rate-limited)")
            elif mbps[ph]:
                extra = ""
                idle = (summarize(rtts.get("ref", []))["avg"]) if rtts.get("ref") else None
                if vals and idle is not None:
                    extra = f" · ping {sum(vals) / len(vals) - idle:+.0f} ms"
                d[ph] = f"{mbps[ph]:.0f} Mbps{extra}"
            elif steps[ph] == "run":
                d[ph] = "Waiting for data…" if len(loaded[ph]) >= 8 else "Starting…"
            else:
                d[ph] = ""
        bits = []
        if w.get("connected"):
            bits.append(f"{w['rssi_dbm']:.0f} dBm" if w.get("rssi_dbm") is not None else "Wi-Fi")
        if sysm.get("cpu") is not None:
            bits.append(f"CPU {sysm['cpu']:.0f}%")
        if sysm.get("vpn"):
            bits.append("VPN on")
        d["sys"] = " · ".join(bits)
        return [{"id": sid, "label": label, "state": steps[sid], "detail": d.get(sid, "")}
                for sid, label in STEPS]


def run_full(progress=lambda frac, text: None, live_cb=None, prior=None, cancel=None):
    """Full check. live_cb(snapshot) is called ~4x/s with partial results; prior = last full run's metrics.
    cancel: a threading.Event; once set, the check raises Cancelled at the next probe sample."""
    cfg = load_config()
    live = LiveRun(cfg, prior, cancel)
    started = time.perf_counter()
    total = 2 + IDLE_SECONDS + DOWN_SECONDS + UP_SECONDS
    phase = {"text": "Measuring ping, jitter and packet loss to game datacenters..."}
    done = threading.Event()

    def ticker():
        while not done.wait(0.25):
            progress(min(0.99, (time.perf_counter() - started) / total), phase["text"])
            if live_cb:
                try:
                    live.poll_traffic()
                    live_cb(live.snapshot())
                except Exception:
                    pass
    threading.Thread(target=ticker, daemon=True).start()

    m = {"time": time.time()}
    try:
        with ThreadPoolExecutor(max_workers=24) as ex:
            # Game pings start immediately so the first numbers show up within a second.
            live.ping_started = time.perf_counter()
            live.step("ping", "run")
            srv_futs = [ex.submit(_series, t["host"], t.get("tcp_port"), IDLE_SECONDS, live=live, key=("srv", i))
                        for i, t in enumerate(cfg["servers"])]
            f_ref = ex.submit(_series, cfg["reference_host"], 443, IDLE_SECONDS, live=live, key="ref")
            _on_all_done(srv_futs + [f_ref],
                         lambda: (live.put(ping_done=True), live.step("ping", "done")))

            live.step("net", "run")

            def net_job():
                iface, kind = system.active_interface()
                live.put(connection={"interface": iface, "type": kind})
                live.traffic = system.TrafficSampler(iface)
                live.step("net", "done")
                return iface, kind
            f_net = ex.submit(net_job)

            def gw_job():
                ip = system.default_gateway()
                live.put(gateway_ip=ip)
                if not ip:
                    live.step("router", "skip")
                    return None, None
                live.step("router", "run")
                stats = _series(ip, None, IDLE_SECONDS, live=live, key="gw")
                live.step("router", "done")
                return ip, stats
            f_gw = ex.submit(gw_job)

            f_portal = ex.submit(system.captive_portal)
            _then(f_portal, lambda r: live.put(captive_portal=r))

            live.step("udp", "run")
            f_stun = ex.submit(stun_test, [tuple(s) for s in cfg["stun_servers"]], on_sample=live.add_udp)
            _then(f_stun, lambda r: (live.put(udp=r), live.step("udp", "done")))

            live.step("sys", "run")
            live.put_system(vpn=system.vpn_adapters())
            f_wifi = ex.submit(wifi.wifi_info)
            f_heavy = ex.submit(system.heavy_apps_running)
            f_cpu = ex.submit(system.cpu_percent)
            f_dns = ex.submit(system.dns_time_ms)
            _then(f_wifi, lambda r: live.put(wifi=r))
            _then(f_heavy, lambda r: live.put_system(heavy_apps=r))
            _then(f_cpu, lambda r: live.put_system(cpu=r))
            _then(f_dns, lambda r: live.put_system(dns_ms=r))
            _on_all_done([f_wifi, f_heavy, f_cpu, f_dns], lambda: live.step("sys", "done"))

            iface, kind = f_net.result()
            gw_ip, gw_stats = f_gw.result()
            m["connection"] = {"interface": iface, "type": kind, "gateway": gw_ip}
            m["captive_portal"] = f_portal.result()

            m["servers"] = _aggregate(cfg["servers"], [f.result() for f in srv_futs])
            m["reference"] = f_ref.result()
            m["gateway"] = gw_stats
            m["udp"] = f_stun.result()
            bg_down, bg_up = live.traffic.mbps() if live.traffic else (None, None)
            m["wifi"] = f_wifi.result()
            m["system"] = {"cpu": f_cpu.result(), "dns_ms": f_dns.result(), "vpn": system.vpn_adapters(),
                           "heavy_apps": f_heavy.result(), "bg_down_mbps": bg_down, "bg_up_mbps": bg_up}

        live.check()
        # Throughput with latency under load (bufferbloat)
        if not m["captive_portal"]:
            ref = open_pinger(cfg["reference_host"], 443)
            try:
                phase["text"] = "Testing download speed and lag under load..."
                live.step("down", "run")
                m["download_mbps"], loaded_down = bandwidth.download_test(
                    ref, DOWN_SECONDS, on_sample=lambda rtt, mb: live.add_loaded("down", rtt, mb))
                live.put(download_mbps=m["download_mbps"])
                live.step("down", "done")
                phase["text"] = "Testing upload speed and lag under load..."
                live.step("up", "run")
                m["upload_mbps"], loaded_up = bandwidth.upload_test(
                    ref, UP_SECONDS, on_sample=lambda rtt, mb: live.add_loaded("up", rtt, mb))
                live.put(upload_mbps=m["upload_mbps"])
                live.step("up", "done")
            finally:
                if ref:
                    ref.close()
            m["loaded_down"], m["loaded_up"] = loaded_down, loaded_up
            idle = (m["reference"] or {}).get("avg")
            loaded = [s for s in (loaded_down, loaded_up) if s and s.get("avg") is not None]
            if idle is not None and loaded:
                m["bufferbloat_ms"] = max(s["avg"] for s in loaded) - idle
                m["loaded_loss_pct"] = max(s["loss_pct"] for s in loaded)
        else:
            live.step("down", "skip")
            live.step("up", "skip")
    finally:
        done.set()

    m["network"] = network_label(m)
    progress(1.0, "Done")
    return m


def run_light(last_full, progress=lambda frac, text: None):
    """~6 s watch-mode check: fresh ping/loss/jitter + Wi-Fi; reuses UDP/bufferbloat/bandwidth from last full run."""
    cfg = load_config()
    progress(0.1, "Quick check...")
    m = {k: v for k, v in (last_full or {}).items()
         if k in ("udp", "bufferbloat_ms", "loaded_loss_pct", "download_mbps", "upload_mbps", "system")}
    m["time"] = time.time()
    iface, kind = system.active_interface()
    gw_ip = system.default_gateway()
    m["connection"] = {"interface": iface, "type": kind, "gateway": gw_ip}
    with ThreadPoolExecutor(max_workers=12) as ex:
        srv_futs = [ex.submit(_series, t["host"], t.get("tcp_port"), None, 30, 0.2) for t in cfg["servers"]]
        f_gw = ex.submit(_series, gw_ip, None, None, 30, 0.2) if gw_ip else None
        f_wifi = ex.submit(wifi.wifi_info)
        m["servers"] = _aggregate(cfg["servers"], [f.result() for f in srv_futs])
        m["gateway"] = f_gw.result() if f_gw else None
        m["wifi"] = f_wifi.result()
    m["network"] = network_label(m)
    progress(1.0, "Done")
    return m
