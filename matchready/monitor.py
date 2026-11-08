"""Day-long monitor: what gets recorded every minute, how a day of samples is diagnosed, and the report
that can be handed to whoever maintains the network.

Everything here is pure (no Tk, no network) so the analysis can be tested on synthetic days.
"""
import csv
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SAMPLE_EVERY_S = 60             # one quick check (30 pings per server + router) a minute
FULL_EVERY_S = 3 * 3600         # a full check with a speed test every 3 hours: bufferbloat at different times
DURATIONS = {"1 hour": 3600, "8 hours": 8 * 3600, "24 hours": 24 * 3600}
DEFAULT_DURATION = "24 hours"

GO, RISKY = 80, 60
LOSS_BAD = 2.0                  # % packet loss that ruins a match on its own
SPIKE_MS = 40                   # a sample whose latency is this far above the day's normal counts as a lag spike
GAP_FACTOR = 3                  # no sample for 3 intervals = the PC slept or the app was closed
ROUTER_BAD = {"loss": 1.0, "jitter": 8.0, "avg": 30.0}   # router ping this bad means the problem is local
RSSI_WEAK = -70


# ---------- recording ----------
def sample_from(m, results, kind):
    """Flatten one check into the fields the analysis needs. Missing data stays None."""
    s = {"ts": m.get("time"), "kind": kind, "score": results["score"], "network": m.get("network")}
    g = m.get("servers") or {}
    s.update(ms=g.get("latency"), jitter=g.get("jitter"), loss=g.get("loss_pct"), spikes=g.get("spike_pct"),
             server=g.get("primary"))
    gw = m.get("gateway") or {}
    s["router_ms"], s["router_jitter"], s["router_loss"] = gw.get("avg"), gw.get("jitter"), gw.get("loss_pct")
    conn = m.get("connection") or {}
    s["conn"] = conn.get("type")
    w = m.get("wifi") or {}
    if conn.get("type") == "wifi":
        s.update(rssi=w.get("rssi_dbm"), band=w.get("band"), channel=w.get("channel"), ssid=w.get("ssid"),
                 bssid=w.get("bssid"))
    if kind == "full":
        s.update(bufferbloat=m.get("bufferbloat_ms"), down=m.get("download_mbps"), up=m.get("upload_mbps"))
    return s


def failed_sample(ts, error):
    """A check that couldn't run at all (no network, adapter down). Counts as a full dropout."""
    return {"ts": ts, "kind": "error", "score": 0, "loss": 100.0, "error": error}


# ---------- analysis helpers ----------
def _num(vals):
    return [v for v in vals if isinstance(v, (int, float))]


def _pct(vals, q):
    v = sorted(_num(vals))
    if not v:
        return None
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def _median(vals):
    v = _num(vals)
    return statistics.median(v) if v else None


# Samples saved by older versions had two per-game sets of fields (rl_*, ow_*); read those as the worse of
# the two so old monitor sessions still load and report the same way.
def _field(s, key, old_keys, pick):
    if key in s:
        return s[key] if isinstance(s[key], (int, float)) else None
    v = _num([s.get(k) for k in old_keys])
    return pick(v) if v else None


def score(s):
    return _field(s, "score", ("rl", "ow"), min) or 0


def latency(s):
    return _field(s, "ms", ("rl_ms", "ow_ms"), max)


def loss(s):
    return _field(s, "loss", ("rl_loss", "ow_loss"), max)


def jitter(s):
    return _field(s, "jitter", ("rl_jitter", "ow_jitter"), max)


def is_dropout(s):
    return s.get("kind") == "error" or (loss(s) or 0) >= 100


def is_bad(s, base_ms=None):
    """Bad enough that a ranked match would suffer."""
    if is_dropout(s) or score(s) < RISKY or (loss(s) or 0) >= LOSS_BAD:
        return True
    lat = latency(s)
    return base_ms is not None and lat is not None and lat > base_ms + SPIKE_MS


def router_bad(s):
    if s.get("router_ms") is None and s.get("router_loss") is None:
        return None  # no router reading: can't say
    return ((s.get("router_loss") or 0) >= ROUTER_BAD["loss"] or (s.get("router_jitter") or 0) >= ROUTER_BAD["jitter"]
            or (s.get("router_ms") or 0) >= ROUTER_BAD["avg"])


def hour_label(h):
    return datetime(2000, 1, 1, h).strftime("%I%p").lstrip("0").lower()


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def when(ts):
    return datetime.fromtimestamp(ts).strftime("%a %b %d, ") + clock(ts)


def dur(seconds):
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    m = seconds // 60
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


# ---------- analysis ----------
def incidents(samples, base_ms, interval=SAMPLE_EVERY_S):
    """Runs of consecutive bad samples. One good sample between two bad ones doesn't split an incident,
    and a gap in recording does."""
    out, cur, good_streak = [], None, 0
    for s in samples:
        if cur and s["ts"] - cur["last_ts"] > GAP_FACTOR * interval:
            out.append(cur)
            cur, good_streak = None, 0
        if is_bad(s, base_ms):
            if cur is None:
                cur = {"start": s["ts"], "samples": []}
            cur["samples"].append(s)
            cur["last_ts"] = s["ts"]
            good_streak = 0
        elif cur:
            good_streak += 1
            if good_streak >= 2:
                out.append(cur)
                cur, good_streak = None, 0
    if cur:
        out.append(cur)
    result = []
    for inc in out:
        ss = inc["samples"]
        rb = [router_bad(s) for s in ss]
        known = [r for r in rb if r is not None]
        result.append({
            "start": inc["start"], "end": inc["last_ts"] + interval,
            "duration": inc["last_ts"] + interval - inc["start"], "samples": len(ss),
            "dropout": any(is_dropout(s) for s in ss),
            "worst_score": min(score(s) for s in ss),
            "peak_loss": max(_num([loss(s) for s in ss]) or [0]),
            "peak_ms": max(_num([latency(s) for s in ss]) or [0]) or None,
            "local": (sum(known) / len(known) >= 0.5) if known else None,
        })
    return result


def gaps(samples, interval=SAMPLE_EVERY_S):
    return [(a["ts"], b["ts"]) for a, b in zip(samples, samples[1:]) if b["ts"] - a["ts"] > GAP_FACTOR * interval]


def hourly(samples, base_ms):
    by = defaultdict(list)
    for s in samples:
        by[datetime.fromtimestamp(s["ts"]).hour].append(s)
    out = {}
    for h, ss in by.items():
        out[h] = {"n": len(ss), "bad_pct": 100.0 * sum(is_bad(s, base_ms) for s in ss) / len(ss),
                  "score": _median([score(s) for s in ss]), "ms": _median([latency(s) for s in ss]),
                  "jitter": _median([jitter(s) for s in ss]),
                  "loss": statistics.fmean(_num([loss(s) for s in ss]) or [0]),
                  "dropouts": sum(is_dropout(s) for s in ss)}
    return out


def _spans(hours):
    """[3, 4, 5, 9] -> [(3, 5), (9, 9)]; wraps past midnight."""
    hs = sorted(set(hours))
    spans = []
    for h in hs:
        if spans and h == spans[-1][1] + 1:
            spans[-1][1] = h
        else:
            spans.append([h, h])
    if len(spans) > 1 and spans[0][0] == 0 and spans[-1][1] == 23:
        spans[0][0] = spans.pop()[0]
    return [tuple(s) for s in spans]


def span_label(a, b):
    return f"{hour_label(a)}-{hour_label((b + 1) % 24)}"


def analyze(samples, interval=SAMPLE_EVERY_S):
    """Diagnose a monitored period. Returns plain data; report_text() and the UI turn it into words."""
    samples = sorted((s for s in samples if s.get("ts")), key=lambda s: s["ts"])
    if not samples:
        return {"n": 0}
    ok = [s for s in samples if not is_dropout(s)]
    base_ms = _median([latency(s) for s in ok])
    bad = [s for s in samples if is_bad(s, base_ms)]
    n = len(samples)
    a = {"n": n, "start": samples[0]["ts"], "end": samples[-1]["ts"] + interval, "base_ms": base_ms}
    a["covered_s"] = n * interval
    a["gaps"] = gaps(samples, interval)
    a["networks"] = sorted({s["network"] for s in samples if s.get("network")})
    a["go_pct"] = 100.0 * sum(score(s) >= GO for s in samples) / n
    a["risky_pct"] = 100.0 * sum(RISKY <= score(s) < GO for s in samples) / n
    a["bad_pct"] = 100.0 * len(bad) / n
    a["dropouts"] = sum(is_dropout(s) for s in samples)
    a["ms_p50"], a["ms_p95"], a["ms_p99"] = (_pct([latency(s) for s in ok], q) for q in (0.5, 0.95, 0.99))
    a["ms_max"] = max(_num([latency(s) for s in ok]) or [0]) or None
    a["jitter_p50"], a["jitter_p95"] = _pct([jitter(s) for s in ok], 0.5), _pct([jitter(s) for s in ok], 0.95)
    a["loss_avg"] = statistics.fmean(_num([loss(s) for s in samples]) or [0])
    a["loss_samples_pct"] = 100.0 * sum((loss(s) or 0) > 0 for s in samples) / n
    a["spike_samples"] = sum(1 for s in ok if base_ms is not None and (latency(s) or 0) > base_ms + SPIKE_MS)
    a["incidents"] = incidents(samples, base_ms, interval)
    a["hours"] = hourly(samples, base_ms)

    # Where is the problem? Compare the router (first hop) with the game servers during bad samples.
    rb = [router_bad(s) for s in bad]
    known = [r for r in rb if r is not None]
    a["local_share"] = (sum(known) / len(known)) if known else None
    good = [s for s in samples if not is_bad(s, base_ms)]
    a["router_p95"] = _pct([s.get("router_ms") for s in samples], 0.95)
    a["router_loss_avg"] = statistics.fmean(_num([s.get("router_loss") for s in samples]) or [0])
    wifi = [s for s in samples if s.get("conn") == "wifi"]
    a["wifi_pct"] = 100.0 * len(wifi) / n
    a["rssi_good"] = _median([s.get("rssi") for s in good])
    a["rssi_bad"] = _median([s.get("rssi") for s in bad])
    a["rssi_min"] = min(_num([s.get("rssi") for s in samples]) or [0]) or None
    a["roams"] = sum(1 for x, y in zip(wifi, wifi[1:]) if x.get("bssid") and y.get("bssid")
                     and x["bssid"] != y["bssid"])
    full = [s for s in samples if s.get("kind") == "full" and s.get("bufferbloat") is not None]
    a["bufferbloat"] = [(s["ts"], s["bufferbloat"]) for s in full]

    # Time-of-day pattern: hours clearly worse than the rest of the day. Needs enough data per hour.
    hours = a["hours"]
    enough = {h: v for h, v in hours.items() if v["n"] >= max(5, 0.25 * 3600 / interval)}
    overall_bad = a["bad_pct"]
    worse = [h for h, v in enough.items()
             if v["bad_pct"] >= max(15.0, 2 * overall_bad)
             or (base_ms is not None and v["ms"] is not None and v["ms"] >= base_ms + 20)]
    a["bad_spans"] = _spans(worse)
    a["best_hours"] = sorted((h for h, v in enough.items() if v["bad_pct"] == 0 and h not in worse),
                             key=lambda h: hours[h]["ms"] or 0)[:3]
    a["hours_covered"] = len(hours)
    a["days"] = len({datetime.fromtimestamp(s["ts"]).date() for s in samples})

    a["findings"] = findings(a)
    a["rating"], a["rating_text"] = rating(a)
    a["maintenance"] = maintenance(a)
    return a


def rating(a):
    incs = a["incidents"]
    long_incs = [i for i in incs if i["duration"] >= 300]
    if a["bad_pct"] < 2 and a["dropouts"] <= 1 and a["loss_avg"] < 0.5 and not long_incs:
        return "stable", "Stable. Good enough for ranked all day."
    if a["bad_pct"] >= 20 or a["dropouts"] >= 10 or a["loss_avg"] >= 2:
        return "unstable", "Unstable. Problems were frequent enough to ruin matches throughout the period."
    if a["bad_spans"]:
        return "pattern", "Unstable at certain times. Mostly fine, but it got worse at particular hours."
    return "flaky", "Occasionally unstable. Problems came and went without a clear time pattern."


def findings(a):
    """Plain-language observations, most important first."""
    out = []
    incs = a["incidents"]
    if a["bad_spans"]:
        spans = ", ".join(span_label(x, y) for x, y in a["bad_spans"])
        hrs = [a["hours"][h] for x, y in a["bad_spans"] for h in _range(x, y) if h in a["hours"]]
        bad_pct = statistics.fmean([v["bad_pct"] for v in hrs]) if hrs else 0
        out.append(("pattern", f"The connection gets worse around {spans}: {bad_pct:.0f}% of checks in those hours "
                               f"were bad, against {a['bad_pct']:.0f}% across the whole period."
                               + ("" if a["end"] - a["start"] > 36 * 3600 else
                                  " Run the monitor on another day too: if it happens at the same time again, "
                                  "it's a pattern rather than a one-off.")))
        lat_hrs = [v["ms"] for v in hrs if v["ms"] is not None]
        if a["base_ms"] and lat_hrs and statistics.fmean(lat_hrs) >= a["base_ms"] + 15:
            out.append(("congestion", f"Ping rises from a normal {a['base_ms']:.0f} ms to about "
                                      f"{statistics.fmean(lat_hrs):.0f} ms at those times. A rise like this at busy "
                                      "hours usually means the network is overloaded when many people use it "
                                      "(congestion), not a fault in your PC."))
    elif incs:
        out.append(("random", "Problems didn't cluster at particular hours. Random drops point to an "
                              "intermittent fault (loose cable, failing equipment, interference) rather than "
                              "busy-hour congestion."))
    if a["dropouts"]:
        out.append(("dropouts", f"The connection dropped out completely {a['dropouts']} time"
                                f"{'s' if a['dropouts'] != 1 else ''} (no game server answered at all)."))
    if a["local_share"] is not None and incs:
        if a["local_share"] >= 0.5:
            out.append(("local", f"In {a['local_share'] * 100:.0f}% of bad checks your own router was slow or "
                                 "dropping packets too. The problem is between this PC and the router: Wi-Fi "
                                 "signal, the access point, or the cable."))
        else:
            out.append(("upstream", f"Your router answered normally during {100 - a['local_share'] * 100:.0f}% of "
                                    "bad checks, so the problem is past your router: the building or campus "
                                    "network, or the internet provider."))
    if a["wifi_pct"] >= 50 and a["rssi_bad"] is not None and a["rssi_good"] is not None \
            and a["rssi_bad"] <= a["rssi_good"] - 6:
        out.append(("wifi", f"Wi-Fi signal was weaker when things went wrong ({a['rssi_bad']:.0f} dBm vs "
                            f"{a['rssi_good']:.0f} dBm when fine). Moving closer to the access point or using "
                            "Ethernet would likely help."))
    elif a["wifi_pct"] >= 50 and a["rssi_min"] is not None and a["rssi_min"] <= RSSI_WEAK:
        out.append(("wifi", f"Wi-Fi signal dipped as low as {a['rssi_min']:.0f} dBm, which is weak for gaming."))
    if a["roams"] >= 3:
        out.append(("roam", f"Your PC switched between Wi-Fi access points {a['roams']} times. Each switch can "
                            "cause a short lag spike or disconnect."))
    if a["loss_avg"] >= 0.5:
        out.append(("loss", f"Average packet loss was {a['loss_avg']:.1f}% (games want 0%; above 2% is "
                            "unplayable). {:.0f}% of checks lost at least one packet.".format(a["loss_samples_pct"])))
    bb = [v for _, v in a["bufferbloat"]]
    if bb and max(bb) >= 60:
        out.append(("bufferbloat", f"Lag under load reached +{max(bb):.0f} ms. Any download or stream on the "
                                   "network makes games lag."))
    if a["gaps"]:
        missing = sum(b - a_ for a_, b in a["gaps"])
        out.append(("gaps", f"Nothing was recorded for {dur(missing)} in total (the PC slept or MatchReady was "
                            "closed), so those times aren't covered."))
    if not out:
        out.append(("fine", "No problems found. Ping, jitter and packet loss stayed in the healthy range."))
    return out


def _range(a, b):
    return list(range(a, b + 1)) if a <= b else list(range(a, 24)) + list(range(0, b + 1))


def maintenance(a):
    """Is a maintenance request justified, and who should get it."""
    incs = a["incidents"]
    justified = a["rating"] in ("unstable", "pattern", "flaky") and (len(incs) >= 3 or a["dropouts"] >= 2
                                                                       or a["loss_avg"] >= 1 or a["bad_pct"] >= 5)
    if a["local_share"] is None:
        who = "network maintenance"
    elif a["local_share"] >= 0.5:
        who = "whoever maintains the Wi-Fi / router where you are (landlord, dorm or campus IT)"
    else:
        who = "the building or campus network team, or your internet provider"
    return {"justified": justified, "who": who}


# ---------- report ----------
def report_text(a, meta=None):
    """A self-contained summary someone can paste into a maintenance ticket or email."""
    meta = meta or {}
    if not a.get("n"):
        return "No samples were recorded."
    L = []
    L.append("CONNECTION STABILITY REPORT (MatchReady)")
    L.append("=" * 44)
    L.append(f"Period:   {when(a['start'])} to {when(a['end'])} ({dur(a['end'] - a['start'])})")
    if a["networks"]:
        L.append(f"Network:  {', '.join(a['networks'])}")
    if meta.get("location"):
        L.append(f"Location: {meta['location']}")
    L.append(f"Method:   a quick check every {SAMPLE_EVERY_S} s ({a['n']} checks): 30 pings each to nearby game datacenters "
             "(Chicago, Dallas, Iowa) and to the local router, plus a speed test every "
             f"{FULL_EVERY_S // 3600} hours.")
    L.append("")
    L.append(f"VERDICT: {a['rating_text']}")
    L.append("")
    L.append("KEY NUMBERS")
    L.append(f"  Time usable for online games ...... {a['go_pct']:.1f}%")
    L.append(f"  Time with problems ................ {a['bad_pct']:.1f}%")
    L.append(f"  Separate problem periods .......... {len(a['incidents'])}")
    L.append(f"  Complete dropouts ................. {a['dropouts']}")
    if a["ms_p50"] is not None:
        L.append(f"  Ping (median / 95th / worst) ...... {a['ms_p50']:.0f} / {a['ms_p95']:.0f} / {a['ms_max']:.0f} ms")
    if a["jitter_p50"] is not None:
        L.append(f"  Jitter (median / 95th) ............ {a['jitter_p50']:.1f} / {a['jitter_p95']:.1f} ms")
    L.append(f"  Packet loss (average) ............. {a['loss_avg']:.2f}%  (healthy: 0%)")
    L.append(f"  Checks with any packet loss ....... {a['loss_samples_pct']:.1f}%")
    if a["router_p95"] is not None:
        L.append(f"  Router ping (95th) / loss ......... {a['router_p95']:.1f} ms / {a['router_loss_avg']:.2f}%")
    if a["wifi_pct"] >= 50 and a["rssi_min"] is not None:
        L.append(f"  Wi-Fi signal (worst) .............. {a['rssi_min']:.0f} dBm")
    L.append("")
    L.append("FINDINGS")
    for _, text in a["findings"]:
        L.append(f"  - {text}")
    L.append("")
    L.append("BY HOUR OF DAY (share of checks with problems, median ping)")
    for h in sorted(a["hours"]):
        v = a["hours"][h]
        bar = "#" * int(round(v["bad_pct"] / 5))
        ms = f"{v['ms']:.0f} ms" if v["ms"] is not None else "  -  "
        L.append(f"  {hour_label(h):>4}  {v['bad_pct']:5.1f}%  {ms:>7}  {bar}")
    incs = sorted(a["incidents"], key=lambda i: -i["duration"])
    if incs:
        L.append("")
        L.append(f"PROBLEM PERIODS (longest {min(15, len(incs))} of {len(incs)})")
        for i in incs[:15]:
            where = {True: "local (router also affected)", False: "past the router", None: "unknown"}[i["local"]]
            what = "DROPOUT" if i["dropout"] else f"loss up to {i['peak_loss']:.0f}%, ping up to {i['peak_ms'] or 0:.0f} ms"
            L.append(f"  {when(i['start'])}  {dur(i['duration']):>10}  {what}  [{where}]")
    mt = a["maintenance"]
    L.append("")
    L.append("REQUEST")
    if mt["justified"]:
        L.append(f"  These results justify asking {mt['who']} to investigate.")
        if a["bad_spans"]:
            L.append("  Please check the network around " + ", ".join(span_label(x, y) for x, y in a["bad_spans"])
                     + ", when the problems repeat.")
        L.append("  The attached CSV has every individual measurement with timestamps.")
    else:
        L.append("  The connection was mostly healthy over this period; there isn't strong evidence for a fault.")
    return "\n".join(L)


CSV_FIELDS = ["time", "kind", "score", "ping_ms", "jitter_ms", "loss_pct", "server", "router_ms",
              "router_jitter_ms", "router_loss_pct", "connection", "wifi_rssi_dbm", "wifi_band", "wifi_channel",
              "wifi_bssid", "bufferbloat_ms", "download_mbps", "upload_mbps", "error"]


def csv_row(s):
    vals = [s.get("kind"), score(s), latency(s), jitter(s), loss(s), s.get("server")]
    vals += [s.get(k) for k in ("router_ms", "router_jitter", "router_loss", "conn", "rssi", "band", "channel",
                                "bssid", "bufferbloat", "down", "up", "error")]
    out = [datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d %H:%M:%S")]
    for v in vals:
        out.append(f"{v:.1f}" if isinstance(v, float) else "" if v is None else v)
    return out


def export(samples, a, folder, stem):
    """Write <stem>.txt (the report) and <stem>.csv (every sample). Returns both paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    txt, csv_path = folder / f"{stem}.txt", folder / f"{stem}.csv"
    txt.write_text(report_text(a), encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for s in sorted(samples, key=lambda s: s["ts"]):
            w.writerow(csv_row(s))
    return txt, csv_path
