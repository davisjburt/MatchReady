import threading
import time

import pytest

from matchready import runner, updater
from matchready.probes import bandwidth


def test_clean_env_drops_pyinstaller_state(monkeypatch):
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI123")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI123")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = updater.clean_env()
    assert not any(k.upper().startswith(("_PYI_", "_MEIPASS")) for k in env)
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert env["KEEP_ME"] == "yes"


def test_live_run_raises_once_cancelled():
    cancel = threading.Event()
    live = runner.LiveRun(runner.load_config(), cancel=cancel)
    add = live.sink("x")
    add(12.0)  # fine before cancel
    cancel.set()
    with pytest.raises(runner.Cancelled):
        add(13.0)
    with pytest.raises(runner.Cancelled):
        live.add_udp(5.0)
    with pytest.raises(runner.Cancelled):
        live.add_loaded("down", 20.0, 50.0)


def test_load_streams_stop_when_on_sample_raises(monkeypatch):
    monkeypatch.setattr(bandwidth, "WARMUP", 0.0)
    stopped = []

    def worker(counter, stop):
        while not stop.is_set():
            counter.add(1000)
            time.sleep(0.01)
        stopped.append(True)

    def on_sample(rtt, mbps):
        raise runner.Cancelled()

    with pytest.raises(runner.Cancelled):
        bandwidth._run_load(worker, 2, 5.0, None, on_sample)
    deadline = time.time() + 2
    while len(stopped) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(stopped) == 2
