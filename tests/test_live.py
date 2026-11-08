import time

from matchready import runner
from matchready.probes import bandwidth
from matchready.probes.latency import ping_series

CFG = {"servers": [{"name": "A", "host": "a"}, {"name": "B", "host": "b"}, {"name": "C", "host": "c"}]}
CLEAN = [20.0, 21.0, 20.5, 19.5, 20.0, 21.5, 20.0, 20.5, 19.8, 20.2]


def feed(live, series_by_key):
    for key, vals in series_by_key.items():
        add = live.sink(key)
        for v in vals:
            add(v)


def clean_run(**kw):
    live = runner.LiveRun(CFG, **kw)
    feed(live, {("srv", 0): CLEAN, ("srv", 1): CLEAN, ("srv", 2): CLEAN, "ref": CLEAN,
                "gw": [1.0, 1.2, 1.1, 1.0]})
    live.put(connection={"interface": "Ethernet", "type": "wired"})
    return live


class FakePinger:
    def __init__(self, values):
        self.values = list(values)

    def probe(self):
        return self.values.pop(0)


def test_ping_series_reports_each_sample():
    seen = []
    out = ping_series(FakePinger([10.0, None, 12.0]), count=3, interval=0, on_sample=seen.append)
    assert out == seen == [10.0, None, 12.0]


def test_no_estimate_until_enough_pings():
    live = runner.LiveRun(CFG)
    feed(live, {("srv", 2): CLEAN[:3]})
    snap = live.snapshot()
    assert snap["estimate"] is None and "servers" not in snap["m"]
    feed(live, {("srv", 2): CLEAN[3:]})
    snap = live.snapshot()
    assert snap["estimate"]["pings"] == 10 and snap["m"]["servers"]["primary"] == "C"


def test_clean_connection_predicts_go():
    est = clean_run().snapshot()["estimate"]
    assert est["verdict"] == "go" and est["score"] >= 80
    assert est["low"] <= est["score"] <= est["high"]


def test_one_early_drop_does_not_trip_the_loss_cap_but_real_loss_does():
    lone = runner.LiveRun(CFG)
    feed(lone, {("srv", 0): [20.0] * 7 + [None]})
    assert lone.snapshot()["estimate"]["caps"] == []

    lossy = runner.LiveRun(CFG)
    feed(lossy, {("srv", 0): [20.0, None, 20.0, 21.0, None, 20.0, 20.0, None, 20.0, 20.0]})
    est = lossy.snapshot()["estimate"]
    assert est["score"] <= 40 and est["caps"]


def test_range_narrows_as_measurements_arrive():
    live = clean_run()
    live.ping_started = time.perf_counter()
    early = live.snapshot()["estimate"]["spread"]
    live.ping_started = time.perf_counter() - runner.IDLE_SECONDS
    live.put(ping_done=True)
    mid = live.snapshot()["estimate"]["spread"]
    live.put(udp={"ok": True, "loss_pct": 0.0, "nat": "Open/Moderate", "sent": 40, "received": 40})
    feed(live, {})
    live.add_loaded("down", 22.0, 300.0), live.add_loaded("down", 23.0, 310.0), live.add_loaded("down", 22.0, 305.0)
    late = live.snapshot()["estimate"]["spread"]
    assert early > mid > late


def test_live_bufferbloat_and_prior_stand_in():
    prior = {"connection": {"interface": "Ethernet"}, "bufferbloat_ms": 80.0, "loaded_loss_pct": 0.0,
             "udp": {"ok": True, "loss_pct": 0.0}, "download_mbps": 200.0}
    live = clean_run(prior=prior)
    snap = live.snapshot()
    assert snap["m"]["bufferbloat_ms"] == 80.0               # used for the prediction...
    assert "bufferbloat_ms" not in snap["view"]               # ...but not shown as a measurement
    for v in (60.0, 65.0, 70.0):
        live.add_loaded("down", v, 250.0)
    snap = live.snapshot()
    assert round(snap["m"]["bufferbloat_ms"]) == 45           # measured: loaded ~65 ms vs idle ~20 ms
    assert "bufferbloat_ms" in snap["view"]


def test_prior_from_another_interface_is_ignored():
    prior = {"connection": {"interface": "Wi-Fi"}, "bufferbloat_ms": 80.0, "udp": {"ok": True, "loss_pct": 0.0}}
    snap = clean_run(prior=prior).snapshot()
    assert "bufferbloat_ms" not in snap["m"] and "udp" not in snap["m"]


def test_captive_portal_predicts_zero_immediately():
    live = clean_run()
    live.put(captive_portal=True)
    est = live.snapshot()["estimate"]
    assert est["score"] == 0 and est["spread"] == 0


def test_activity_and_traces():
    live = clean_run()
    live.step("net", "done")
    live.step("ping", "run")
    snap = live.snapshot()
    rows = {r["id"]: r for r in snap["activity"]}
    assert [r["id"] for r in snap["activity"]] == [sid for sid, _ in runner.STEPS]
    assert rows["net"]["state"] == "done" and "Ethernet" in rows["net"]["detail"]
    assert "pings" in rows["ping"]["detail"] and "best" in rows["ping"]["detail"]
    assert set(snap["traces"]) == {"servers", "router"}
    live.step("down", "run")
    live.add_loaded("down", 40.0, 120.0)
    snap = live.snapshot()
    assert snap["trace_title"] == "Ping while downloading" and snap["traces"] == {"load": [40.0]}
    assert "120 Mbps" in {r["id"]: r for r in snap["activity"]}["down"]["detail"]


def test_failed_speed_test_is_unavailable_not_zero(monkeypatch):
    # Nothing flows (e.g. HTTP 429 from the speed endpoint): report None, never "0 Mbps".
    monkeypatch.setattr(bandwidth, "WARMUP", 0.0)

    def idle_worker(counter, stop):
        stop.wait(5)
    mbps, loaded = bandwidth._run_load(idle_worker, 1, 0.3, None)
    assert mbps is None and loaded is None

    live = clean_run()
    live.step("down", "done")
    live.put(download_mbps=None)
    assert "Unavailable" in {r["id"]: r for r in live.snapshot()["activity"]}["down"]["detail"]
    assert "download_mbps" not in live.snapshot()["m"]
