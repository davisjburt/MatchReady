import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime

import pytest

from matchready.charts import HistoryChart, blend, hour_label, time_ticks

THEME = {"bg": "#101318", "panel": "#181c23", "raised": "#222831", "line": "#3a4250", "text": "#e9ecf1",
         "muted": "#a3abb8", "red": "#ff6b6b", "amber": "#f2b84b", "green": "#3ddc97", "series": "#5b9bff",
         "font": "Segoe UI", "risky": 60, "go": 80}


def ts(*args):
    return datetime(*args).timestamp()


def test_blend_endpoints_and_midpoint():
    assert blend("#ff0000", "#000000", 0) == "#000000"
    assert blend("#ff0000", "#000000", 1) == "#ff0000"
    assert blend("#ffffff", "#000000", 0.5) == "#808080"


def test_hour_labels():
    assert [hour_label(h) for h in (0, 9, 12, 21)] == ["12am", "9am", "12pm", "9pm"]


def test_time_ticks_pick_clock_weekday_or_date_labels():
    day = time_ticks(ts(2026, 10, 9, 9), ts(2026, 10, 9, 21))
    assert len(day) == 6 and day[0][1] == "9:00am" and day[-1][1] == "9:00pm"
    two_days = time_ticks(ts(2026, 10, 8, 9), ts(2026, 10, 10, 21))
    assert two_days[0][1] == "Thu 9am"
    weeks = time_ticks(ts(2026, 10, 1), ts(2026, 10, 14))
    assert weeks[0][1] == "Oct 1" and weeks[-1][1] == "Oct 14"
    assert time_ticks(ts(2026, 10, 9, 9), ts(2026, 10, 9, 9)) == [(ts(2026, 10, 9, 9), "9:00am")]


@pytest.fixture(scope="module")
def tk_root():
    # One root for the whole module: repeatedly creating and destroying Tk roots in a single process is flaky.
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"Tk unavailable: {e}")
    r.geometry("900x700")
    yield r
    r.destroy()


@pytest.fixture
def root(tk_root):
    frame = tk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    yield frame
    frame.destroy()


def draw(root, rows, profile):
    chart = HistoryChart(root, THEME)
    chart.pack(fill="both", expand=True)
    root.update()
    chart.set_data(rows, profile)
    return chart


def texts(chart):
    return [chart.itemcget(i, "text") for i in chart.find_all() if chart.type(i) == "text"]


def test_empty_state(root):
    chart = draw(root, [], {})
    assert texts(chart).count("No checks yet. Run one from the Check tab.") == 2
    assert not [i for i in chart.find_all() if chart.type(i) == "oval"]


def test_draws_series_markers_bars_and_labels(root):
    rows = [(ts(2026, 10, 9, h), "full", 90 - h) for h in range(9, 15)]
    chart = draw(root, rows, {9: 80, 12: 55, 14: 92})
    shapes = chart.aa.shapes                 # shapes are drawn anti-aliased into one image (see aa.py)
    assert [k for k, _ in shapes].count("oval") == 6          # one circle per check
    assert [chart.type(i) for i in chart.find_all()].count("image") == 1
    assert {"80", "55", "92"} <= set(texts(chart))   # bar value labels
    bar_fills = {col for k, col in shapes if k == "rect"}
    assert bar_fills == {THEME["green"], THEME["red"]}   # 80 and 92 are good, 55 is bad


def test_smooth_curve_passes_through_points_without_overshooting():
    from matchready.aa import smooth
    pts = [(0, 10), (20, 50), (40, 50), (60, 5), (80, 30)]
    curve = smooth(pts)
    assert len(curve) > len(pts) and curve[0] == pts[0] and curve[-1] == pts[-1]
    for p in pts:
        assert any(abs(q[0] - p[0]) < 1e-9 and abs(q[1] - p[1]) < 1e-9 for q in curve)
    ys = [y for _, y in curve]
    assert min(ys) >= 5 - 1e-9 and max(ys) <= 50 + 1e-9   # never past the measured peaks
    flat = [y for x, y in curve if 20 <= x <= 40]
    assert all(abs(y - 50) < 1e-9 for y in flat)            # a flat stretch stays flat
    dense = [(i, i % 2) for i in range(100)]                # points closer than the step are left alone
    assert smooth(dense) == dense


def test_single_point_and_none_scores_do_not_crash(root):
    draw(root, [(ts(2026, 10, 9, 9), "full", 88)], {9: 88})
    draw(root, [(ts(2026, 10, 9, 9), "light", None), (ts(2026, 10, 9, 10), "full", 80)], {})


def test_gui_import_does_not_pull_in_matplotlib_or_numpy():
    # They added ~60 MB to the exe and ~20 s to every auto-update build; keep them out.
    code = "import sys; import matchready.gui; print('matplotlib' in sys.modules or 'numpy' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_gauge_angles_and_needle_animation(root):
    from matchready.gauge import Gauge, angle_for
    assert angle_for(0) == 135 and angle_for(100) == 405 and angle_for(50) == 270  # 50 points straight up
    assert angle_for(-5) == 135 and angle_for(150) == 405
    g = Gauge(root, {**THEME, "text": THEME["text"]})
    g.pack()
    g.clear(text="GO", caption="click to run a check", color=THEME["green"])
    assert g.itemcget(g.number, "text") == "GO"
    g.set(72, THEME["amber"], 64, 80, live=True)
    for _ in range(200):  # the needle eases in on 16 ms timer steps
        time.sleep(0.02)
        root.update()
        if g.shown == g.target:
            break
    assert g.shown == 72
    assert g.itemcget(g.number, "text") == "~72" and g.itemcget(g.caption, "text") == "± 8"
    g.set(90, THEME["green"])
    assert g.itemcget(g.caption, "text") == "/ 100"
    g.clear()
    assert g.shown is None and g.itemcget(g.number, "text") == "--"
