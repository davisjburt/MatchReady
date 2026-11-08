import random
from datetime import datetime

from matchready import monitor
from matchready.storage import History


def day(make, start=datetime(2026, 10, 12, 9, 0), minutes=24 * 60, seed=1):
    """One sample per minute; make(hour, minute_index, rng) returns the fields that differ from a healthy check."""
    rng = random.Random(seed)
    t0 = start.timestamp()
    out = []
    for i in range(minutes):
        ts = t0 + 60 * i
        s = {"ts": ts, "kind": "light", "score": 91, "ms": 22 + rng.random() * 5, "jitter": 1.8, "loss": 0.0,
             "router_ms": 2.0, "router_jitter": 0.5, "router_loss": 0.0, "conn": "wifi", "rssi": -55,
             "bssid": "aa", "network": "Wi-Fi: Campus"}
        s.update(make(datetime.fromtimestamp(ts).hour, i, rng) or {})
        out.append(s)
    return out


def evening_congestion(hour, i, rng):
    if 20 <= hour <= 22:
        return {"score": 45, "ms": 80 + rng.random() * 15, "loss": 3.3}


def test_healthy_day_is_stable_and_needs_no_ticket():
    a = monitor.analyze(day(lambda h, i, r: None))
    assert a["rating"] == "stable"
    assert a["incidents"] == [] and a["dropouts"] == 0
    assert a["go_pct"] == 100
    assert not a["maintenance"]["justified"]
    assert a["findings"][0][0] == "fine"


def test_evening_congestion_is_found_as_a_time_pattern_past_the_router():
    a = monitor.analyze(day(evening_congestion))
    assert a["rating"] == "pattern"
    assert a["bad_spans"] == [(20, 22)]
    kinds = [k for k, _ in a["findings"]]
    assert kinds[0] == "pattern" and "congestion" in kinds and "upstream" in kinds
    assert a["local_share"] == 0
    assert a["maintenance"]["justified"]
    assert "campus" in a["maintenance"]["who"]
    assert 12 <= a["bad_pct"] <= 13  # 3 of 24 hours
    # the evening is one incident per night, not 180 separate ones
    assert len(a["incidents"]) == 1 and a["incidents"][0]["duration"] == 3 * 3600


def test_random_drops_with_a_struggling_router_point_at_local_wifi():
    def flaky(hour, i, rng):
        if i % 97 == 0:  # roughly every hour and a half, all day
            return {"score": 20, "loss": 30.0, "router_loss": 25.0, "router_jitter": 30,
                    "rssi": -78}
    a = monitor.analyze(day(flaky))
    kinds = [k for k, _ in a["findings"]]
    assert "random" in kinds and "local" in kinds and "wifi" in kinds
    assert a["bad_spans"] == []
    assert a["local_share"] == 1
    assert "router" in a["maintenance"]["who"].lower() or "wi-fi" in a["maintenance"]["who"].lower()


def test_dropouts_and_failed_checks_count_as_outages():
    samples = day(lambda h, i, r: {"loss": 100.0, "ms": None, "score": 0} if i in (100, 101, 600) else None)
    samples.append(monitor.failed_sample(samples[-1]["ts"] + 60, "network unreachable"))
    a = monitor.analyze(samples)
    assert a["dropouts"] == 4
    assert sum(i["dropout"] for i in a["incidents"]) == 3  # 100-101 merge into one


def test_gaps_split_incidents_and_are_reported():
    samples = day(evening_congestion, minutes=12 * 60)
    samples = [s for s in samples if not (180 <= (s["ts"] - samples[0]["ts"]) / 60 < 300)]  # 2 h asleep
    a = monitor.analyze(samples)
    assert len(a["gaps"]) == 1
    assert any(k == "gaps" for k, _ in a["findings"])


def test_spans_wrap_past_midnight():
    assert monitor._spans([22, 23, 0, 1, 9]) == [(22, 1), (9, 9)]
    assert monitor.span_label(22, 1) == "10pm-2am"


def test_report_and_export(tmp_path):
    samples = day(evening_congestion)
    samples[30].update(kind="full", bufferbloat=120.0, down=40.0, up=8.0)
    a = monitor.analyze(samples)
    text = monitor.report_text(a)
    for needle in ("CONNECTION STABILITY REPORT", "VERDICT: Unstable at certain times", "8pm-11pm",
                   "PROBLEM PERIODS", "justify asking"):
        assert needle in text
    txt, csv_path = monitor.export(samples, a, tmp_path, "monitor-test")
    assert txt.read_text(encoding="utf-8") == text
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("time,kind,score,ping_ms") and len(lines) == len(samples) + 1


def test_sample_from_flattens_a_check():
    m = {"time": 1000.0, "network": "Wi-Fi: X",
         "servers": {"latency": 20.0, "jitter": 1.0, "loss_pct": 0.5, "spike_pct": 0.0, "primary": "Chicago"},
         "gateway": {"avg": 2.0, "jitter": 0.3, "loss_pct": 0.0},
         "connection": {"type": "wifi"}, "wifi": {"rssi_dbm": -60, "band": "5 GHz", "channel": 36, "bssid": "b"},
         "bufferbloat_ms": 30.0, "download_mbps": 50.0, "upload_mbps": 10.0}
    s = monitor.sample_from(m, {"score": 90}, "full")
    assert s["score"] == 90 and s["ms"] == 20.0 and s["loss"] == 0.5 and s["server"] == "Chicago"
    assert s["router_ms"] == 2.0 and s["rssi"] == -60 and s["bufferbloat"] == 30.0
    assert not monitor.is_dropout(s) and monitor.is_dropout({**s, "loss": 100.0})


def test_old_two_game_samples_still_analyze():
    # Sessions saved before the switch to one score have rl/ow fields: read as the worse of the two.
    old = {"ts": 0, "rl": 92, "ow": 55, "rl_ms": 20.0, "ow_ms": 30.0, "rl_loss": 0.0, "ow_loss": 1.5,
           "rl_jitter": 2.0, "ow_jitter": 4.0}
    assert monitor.score(old) == 55 and monitor.latency(old) == 30.0
    assert monitor.loss(old) == 1.5 and monitor.jitter(old) == 4.0
    assert monitor.is_bad(old)
    row = monitor.csv_row({**old, "ts": 1000.0})
    assert row[2:6] == [55, "30.0", "4.0", "1.5"]


def test_monitor_storage_roundtrip(tmp_path):
    h = History(tmp_path / "h.db")
    sid = h.start_monitor(3600, now=1000.0)
    assert h.unfinished_monitor()[0] == sid
    h.add_monitor_sample(sid, {"ts": 1060.0, "score": 88})
    h.add_monitor_sample(sid, {"ts": 1000.0, "score": 89})
    assert [s["ts"] for s in h.monitor_samples(sid)] == [1000.0, 1060.0]
    h.end_monitor(sid, now=4600.0)
    assert h.unfinished_monitor() is None
    assert h.monitor_sessions()[0][4] == 2


def test_history_keeps_one_score_and_migrates_old_rows(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
                   rl INTEGER, ow INTEGER, network TEXT, metrics TEXT)""")
    con.execute("INSERT INTO runs (ts, kind, rl, ow, network) VALUES (100, 'full', 90, 70, 'N')")
    con.commit()
    con.close()
    h = History(path)
    h.add("light", 85, "N", ts=200)
    assert h.rows(network="N") == [(100, "full", 70), (200, "light", 85)]
    assert History(path).rows() == [(100, "full", 70), (200, "light", 85)]  # migration runs once
