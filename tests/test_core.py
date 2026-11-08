import time
from datetime import datetime

from matchready import predict, scoring
from matchready.probes.latency import summarize
from matchready.probes.udp_stun import parse_mapped_address
from matchready.probes.wifi import parse_interfaces, parse_networks
from matchready.runner import _aggregate, normalize_config

WLAN_IFACES = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 1234
    Physical address       : aa:bb:cc:dd:ee:ff
    Interface type         : Primary
    State                  : connected
    SSID                   : Harding
    AP BSSID               : 11:22:33:44:55:66
    Band                   : 2.4 GHz
    Channel                : 6
    Network type           : Infrastructure
    Radio type             : 802.11n
    Authentication         : WPA2-Enterprise
    Receive rate (Mbps)    : 72.2
    Transmit rate (Mbps)   : 72.2
    Signal                 : 58%
    Rssi                   : -71
    Profile                : Harding
"""

WLAN_NETS = """
SSID 1 : Harding
    BSSID 1                 : 11:22:33:44:55:66
         Signal             : 58%
         Channel            : 6
    BSSID 2                 : 11:22:33:44:55:67
         Channel            : 6
    BSSID 3                 : 11:22:33:44:55:68
         Channel            : 36
SSID 2 : Other
    BSSID 1                 : 99:22:33:44:55:66
         Channel            : 4
"""


def base_metrics(**over):
    servers = {"primary": "X", "latency": 25.0, "jitter": 2.0, "spike_pct": 0.0,
               "loss_pct": 0.0, "max_loss_burst": 0}
    m = {"connection": {"type": "wired"}, "captive_portal": False, "servers": servers,
         "gateway": {"avg": 1.0, "jitter": 0.5, "loss_pct": 0.0},
         "udp": {"ok": True, "loss_pct": 0.0, "nat": "Open/Moderate"},
         "bufferbloat_ms": 5.0, "loaded_loss_pct": 0.0, "download_mbps": 100, "upload_mbps": 20,
         "wifi": {"connected": False}, "system": {"cpu": 10, "vpn": [], "heavy_apps": []}}
    m.update(over)
    return m


def test_curve_interpolates_and_clamps():
    pts = [(0, 100), (10, 50), (20, 0)]
    assert scoring.curve(-5, pts) == 100
    assert scoring.curve(5, pts) == 75
    assert scoring.curve(99, pts) == 0


def test_summarize_loss_jitter_burst():
    s = summarize([20, 22, None, None, None, 20, 24])
    assert round(s["loss_pct"], 1) == 42.9
    assert s["max_loss_burst"] == 3
    assert s["jitter"] == (2 + 2 + 4) / 3
    assert summarize([None, None])["avg"] is None


def test_perfect_connection_is_go():
    r = scoring.score(base_metrics())
    assert r["verdict"] == "go" and r["score"] >= 95


def test_loss_cap_applies():
    m = base_metrics()
    m["servers"]["loss_pct"] = 3.0
    r = scoring.score(m)
    assert r["score"] <= 40 and r["verdict"] == "bad"


def test_weights_cover_every_factor_and_loss_counts_most():
    w = scoring.PROFILE["weights"]
    assert sum(w.values()) == 100
    assert set(w) == {"latency", "jitter", "loss", "bufferbloat", "udp", "wifi"}
    assert max(w, key=w.get) == "loss"


def test_each_core_problem_lowers_the_score():
    good = scoring.score(base_metrics())["score"]
    for field, bad in (("latency", 110.0), ("jitter", 20.0), ("loss_pct", 1.0)):
        m = base_metrics()
        m["servers"][field] = bad
        assert scoring.score(m)["score"] < good - 10, field


def test_typical_good_and_mediocre_connections():
    m = base_metrics()
    m["servers"].update(latency=45.0, jitter=3.0)
    assert scoring.score(m)["verdict"] == "go"
    m["servers"].update(latency=85.0, jitter=12.0, spike_pct=2.0, loss_pct=0.3)
    assert scoring.score(m)["verdict"] in ("risky", "bad")


def test_udp_blocked_and_captive_portal():
    assert scoring.score(base_metrics(udp={"ok": False}))["score"] <= 10
    r = scoring.score(base_metrics(captive_portal=True))
    assert r["score"] == 0 and "sign in" in r["advice"][0]


def test_strict_nat_tip_for_every_game():
    r = scoring.score(base_metrics(udp={"ok": True, "loss_pct": 0.0, "nat": "Strict"}))
    assert any("Strict NAT" in a for a in r["advice"])


def test_local_wifi_problem_gets_wifi_advice():
    m = base_metrics(connection={"type": "wifi"}, gateway={"avg": 15, "jitter": 12, "loss_pct": 2.0},
                     wifi={"connected": True, "band": "2.4 GHz", "rssi_dbm": -78, "aps_same_channel": 7})
    m["servers"].update(jitter=14, loss_pct=1.5, spike_pct=4)
    r = scoring.score(m)
    text = " ".join(r["advice"])
    assert r["verdict"] == "bad"
    assert "Wi-Fi link" in text and "5 GHz" in text


def test_parse_wifi():
    info = parse_interfaces(WLAN_IFACES)
    assert info["connected"] and info["ssid"] == "Harding" and info["channel"] == 6
    assert info["rssi_dbm"] == -71 and info["band"] == "2.4 GHz" and info["rx_mbps"] == 72.2
    assert parse_networks(WLAN_NETS) == {6: 2, 36: 1, 4: 1}
    assert parse_interfaces("There is 1 interface on the system:\n Name : Wi-Fi\n State : disconnected\n"
                            " Description : X\n") == {"connected": False, "adapter": "X"}


def test_stun_xor_mapped_address():
    tid = bytes(range(12))
    # XOR-MAPPED-ADDRESS for 192.0.2.1:32853
    port = 32853 ^ 0x2112
    ip = bytes(b ^ m for b, m in zip(bytes([192, 0, 2, 1]), bytes([0x21, 0x12, 0xA4, 0x42])))
    attr = bytes([0x00, 0x20, 0x00, 0x08, 0x00, 0x01]) + port.to_bytes(2, "big") + ip
    msg = bytes([0x01, 0x01]) + len(attr).to_bytes(2, "big") + bytes([0x21, 0x12, 0xA4, 0x42]) + tid + attr
    assert parse_mapped_address(msg, tid) == ("192.0.2.1", 32853)
    assert parse_mapped_address(msg, bytes(12)) is None


def test_aggregate_uses_nearest_server_and_pools_loss():
    servers = [{"name": "A", "host": "a"}, {"name": "B", "host": "b"}, {"name": "C", "host": "c"}]
    a = {**summarize([30.0] * 10), "method": "icmp"}
    b = {**summarize([10.0] * 9 + [None]), "method": "icmp"}
    c = {**summarize([None] * 5), "method": None}
    g = _aggregate(servers, [a, b, c])
    assert g["primary"] == "B" and g["latency"] == 10.0
    assert g["loss_pct"] == 5.0  # unreachable servers don't count as loss
    assert _aggregate(servers[:1], [c])["latency"] is None


def test_old_per_game_config_is_merged_into_one_pool():
    old = {"games": {"rl": {"name": "RL", "targets": [{"name": "A", "host": "a"}, {"name": "B", "host": "b"}]},
                     "ow": {"name": "OW", "targets": [{"name": "B2", "host": "b"}, {"name": "C", "host": "c"}]}},
           "reference_host": "1.1.1.1"}
    cfg = normalize_config(old)
    assert [s["host"] for s in cfg["servers"]] == ["a", "b", "c"]
    assert cfg["region"] == "US Central" and cfg["reference_host"] == "1.1.1.1"
    new = {"region": "EU West", "servers": [{"name": "X", "host": "x"}]}
    assert normalize_config(new) == new


def test_bundled_targets_json_is_valid():
    from matchready.runner import load_config
    cfg = load_config()
    assert cfg["servers"] and all({"name", "host"} <= set(s) for s in cfg["servers"])
    assert len({s["host"] for s in cfg["servers"]}) == len(cfg["servers"])


def test_forecast_uses_time_slot_history():
    now = datetime(2026, 10, 8, 21, 0).timestamp()  # Thursday 9 pm
    hist = [(now - d * 86400, 50) for d in range(1, 8) if datetime.fromtimestamp(now - d * 86400).weekday() < 5]
    fc = predict.forecast(90, hist, now=now)
    assert fc["score"] < 90 and "usually 50" in fc["text"].lower()
    empty = predict.forecast(85, [], now=time.time())
    assert empty["score"] == 85 and empty["confidence"] == "low"


def test_forecast_trend_down():
    now = time.time()
    hist = [(now - 5400, 95), (now - 3600, 85), (now - 1800, 75)]
    fc = predict.forecast(65, hist, now=now)
    assert fc["score"] < 65 and "getting worse" in fc["text"].lower()
