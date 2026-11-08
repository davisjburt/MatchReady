"""Turns raw measurements into one 0-100 online-gaming score, a verdict and plain-English advice."""


def curve(x, points):
    """Piecewise-linear lookup. points = [(x0, y0), (x1, y1), ...] sorted by x."""
    if x is None:
        return 0.0
    if x <= points[0][0]:
        return float(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(points[-1][1])


# One profile for online games in general. Fast-paced games (shooters, fighting, sports, racing) all suffer
# from the same things: lost packets hurt most (missed inputs, rubber-banding), unstable ping is next
# (most games smooth over steady latency but not swings), then raw ping.
PROFILE = {
    "weights": {"loss": 28, "jitter": 24, "latency": 24, "bufferbloat": 12, "udp": 7, "wifi": 5},
    "latency": [(0, 100), (40, 100), (60, 88), (80, 70), (100, 50), (130, 25), (180, 0)],
    "jitter": [(0, 100), (4, 100), (10, 80), (18, 55), (30, 22), (45, 0)],
    "loss": [(0, 100), (0.25, 92), (0.6, 75), (1.2, 52), (2.5, 22), (4.5, 0)],
    "feel": {
        "go": "Smooth. Fine for ranked and competitive play.",
        "risky": "Playable, but expect occasional rubber-banding or delayed hits. Casual matches are OK; "
                 "ranked is a gamble.",
        "bad": "Expect lag, rubber-banding and possible disconnects. Skip ranked for now.",
    },
}

SPIKES = [(0, 100), (1, 85), (3, 60), (6, 30), (10, 0)]
BUFFERBLOAT = [(0, 100), (10, 95), (30, 75), (60, 45), (100, 20), (200, 0)]
UDP_LOSS = [(0, 100), (3, 85), (10, 45), (25, 0)]
RSSI = [(-90, 0), (-80, 25), (-70, 65), (-65, 85), (-60, 100)]

CORE_FACTORS = ("latency", "jitter", "loss", "udp")
WEAKEST_LINK_POWER = 0.35

GO, RISKY = 80, 60


def verdict_for(score):
    if score >= GO:
        return "go"
    return "risky" if score >= RISKY else "bad"


VERDICT_LABEL = {"go": "GO", "risky": "RISKY", "bad": "DON'T QUEUE"}


def wifi_subscore(m):
    conn = m.get("connection", {}).get("type")
    wifi = m.get("wifi") or {}
    if conn == "wired" or not wifi.get("connected"):
        return 100.0
    s = curve(wifi.get("rssi_dbm"), RSSI) if wifi.get("rssi_dbm") is not None else 70.0
    if (wifi.get("band") or "").startswith("2.4"):
        s -= 15
    same = wifi.get("aps_same_channel")
    if same is not None:
        s -= 20 if same >= 12 else 10 if same >= 6 else 0
    rx = wifi.get("rx_mbps")
    if rx is not None and rx < 50:
        s -= 15
    return max(0.0, s)


def bufferbloat_subscore(m):
    bloat = m.get("bufferbloat_ms")
    if bloat is None:
        return 70.0  # unknown: neutral-ish
    s = curve(max(0.0, bloat), BUFFERBLOAT)
    s -= min(30.0, 3.0 * (m.get("loaded_loss_pct") or 0.0))
    return max(0.0, s)


def udp_subscore(m):
    udp = m.get("udp") or {}
    if not udp:
        return 70.0
    if not udp.get("ok"):
        return 0.0
    return curve(udp.get("loss_pct", 0.0), UDP_LOSS)


def score(m):
    prof = PROFILE
    g = m["servers"]
    result = {"caps": [], "subscores": {}}

    if m.get("captive_portal"):
        result.update(score=0, verdict="bad", caps=["Wi-Fi login page is intercepting traffic"])
        result["advice"] = ["Open a browser and sign in to the network's Wi-Fi login page, then re-run."]
        result["feel"] = "You're not really online yet."
        return result
    if g.get("latency") is None:
        result.update(score=0, verdict="bad", caps=["Couldn't reach any game-region servers"])
        result["advice"] = ["Check that you're connected, and that the network isn't blocking game traffic."]
        result["feel"] = prof["feel"]["bad"]
        return result

    sub = {
        "latency": curve(g["latency"], prof["latency"]),
        "jitter": 0.6 * curve(g["jitter"], prof["jitter"]) + 0.4 * curve(g["spike_pct"], SPIKES),
        "loss": curve(g["loss_pct"], prof["loss"]),
        "bufferbloat": bufferbloat_subscore(m),
        "udp": udp_subscore(m),
        "wifi": wifi_subscore(m),
    }
    w = prof["weights"]
    total = sum(sub[k] * w[k] for k in w) / sum(w.values())
    # Weakest link: one bad core factor ruins a match no matter how good the rest is,
    # so drag the average down by the worst of them (no effect when all are 100).
    weakest = min(sub[k] for k in CORE_FACTORS)
    total *= (max(weakest, 1.0) / 100.0) ** WEAKEST_LINK_POWER

    caps = []
    udp = m.get("udp") or {}
    if udp and not udp.get("ok"):
        caps.append((10, "UDP traffic appears blocked; games can't run without it"))
    if g["loss_pct"] > 2:
        caps.append((40, f"{g['loss_pct']:.1f}% packet loss"))
    if g.get("max_loss_burst", 0) >= 3:
        caps.append((55, f"Connection dropped out for {g['max_loss_burst']} probes in a row"))
    down, up = m.get("download_mbps"), m.get("upload_mbps")
    if (down is not None and down < 1.5) or (up is not None and up < 0.5):
        caps.append((50, "Bandwidth is below what online games need"))
    for cap, _ in caps:
        total = min(total, cap)

    total = int(round(total))
    v = verdict_for(total)
    result.update(score=total, verdict=v, subscores={k: round(x) for k, x in sub.items()},
                  caps=[c for _, c in caps], feel=prof["feel"][v])
    result["advice"] = advise(m, sub, w)
    return result


def advise(m, sub, weights):
    """Ranked fixes: biggest score impact first, followed by always-relevant warnings."""
    g = m["servers"]
    gw = m.get("gateway") or {}
    wifi = m.get("wifi") or {}
    on_wifi = m.get("connection", {}).get("type") == "wifi"
    # Routers often deprioritize pings to themselves, so only blame the local link when the
    # router path shows loss, or jitter comparable to what the game path sees.
    gw_jitter = gw.get("jitter") or 0
    local_bad = (gw.get("loss_pct") or 0) > 0.5 or (gw_jitter > 5 and gw_jitter >= 0.6 * (g["jitter"] or 0)) \
        or ((gw.get("avg") or 0) > 10 and (gw.get("avg") or 0) >= 0.4 * g["latency"])
    tips = []

    def add(factor, text):
        if sub[factor] < 85:
            tips.append((weights[factor] * (100 - sub[factor]), text))

    if local_bad:
        where = "your Wi-Fi link to the access point" if on_wifi else "your local network"
        fix = "Move closer to the access point, switch to 5 GHz, or plug into Ethernet." if on_wifi \
            else "Check the cable and wall port; if it persists, the building network is congested."
        add("loss", f"Packets are being lost on {where} (router ping {gw.get('avg') or 0:.0f} ms, "
                    f"{gw.get('loss_pct') or 0:.1f}% loss). {fix}")
        add("jitter", f"Ping to your own router is unstable ({gw.get('jitter') or 0:.1f} ms jitter). That's "
                      f"interference or congestion on {where}. {fix}")
    else:
        add("loss", f"{g['loss_pct']:.1f}% packet loss past your router: the campus or ISP network is "
                    "congested. Retrying in 15-30 min usually helps.")
        add("jitter", f"Ping is unstable ({g['jitter']:.1f} ms jitter, {g['spike_pct']:.0f}% lag spikes) "
                      "beyond your router. The network is busy right now.")
    add("latency", f"Ping to the nearest game datacenter ({g['primary']}) is {g['latency']:.0f} ms. "
                   + ("Most of that is your local link." if local_bad else
                      "That's route distance or congestion, so there's not much you can fix locally."))
    ll = m.get("loaded_loss_pct") or 0
    add("bufferbloat", f"Ping jumps by {max(0, m.get('bufferbloat_ms') or 0):.0f} ms"
                       + (f" and {ll:.0f}% of packets drop" if ll >= 1 else "")
                       + " when the connection is busy. Pause downloads, streams and cloud sync before queuing.")
    add("udp", "Game (UDP) traffic is being dropped or throttled. If you use a VPN, turn it off; "
               "otherwise this network may filter game traffic.")
    if on_wifi:
        bits = []
        if (wifi.get("band") or "").startswith("2.4"):
            bits.append("you're on 2.4 GHz (switch to the 5 GHz network if there is one)")
        if (wifi.get("rssi_dbm") or 0) < -70:
            bits.append(f"signal is weak ({wifi.get('rssi_dbm'):.0f} dBm)")
        if (wifi.get("aps_same_channel") or 0) >= 6:
            bits.append(f"{wifi['aps_same_channel']} other access points share your channel")
        add("wifi", "Wi-Fi link: " + ("; ".join(bits) if bits else "weak or slow link") +
                    ". Ethernet beats Wi-Fi for competitive games.")

    tips.sort(key=lambda t: -t[0])
    out = [t for _, t in tips[:3]]

    sysm = m.get("system") or {}
    if sysm.get("vpn"):
        out.append(f"VPN active ({', '.join(sysm['vpn'])}). That adds latency; disconnect it for matches.")
    bg = (sysm.get("bg_down_mbps") or 0) + (sysm.get("bg_up_mbps") or 0)
    if bg > 2:
        apps = sysm.get("heavy_apps") or []
        who = f" Likely culprits: {', '.join(apps)}." if apps else ""
        out.append(f"Something on this PC is using {bg:.1f} Mbps in the background.{who}")
    if (sysm.get("cpu") or 0) > 85:
        out.append(f"CPU is at {sysm['cpu']:.0f}%. Stutter may come from your PC, not the network.")
    udp = m.get("udp") or {}
    if udp.get("nat") == "Strict":
        out.append("Strict NAT detected: matches usually work, but joining friends' parties or voice chat can fail.")
    return out
