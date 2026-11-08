"""History charts on a Tk canvas: shapes anti-aliased with Pillow (see aa.py), text as canvas text.

matplotlib used to draw these two charts, but it pulled numpy and Pillow plugins into the exe: ~60 MB and
~20 s on every auto-update build, for two simple plots.
"""
import tkinter as tk
from datetime import datetime

from .aa import AALayer, smooth
from .monitor import latency, loss


def blend(fg, bg, alpha):
    """fg drawn at `alpha` opacity over bg, as a hex colour (Tk canvases have no transparency)."""
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(b[i] + (f[i] - b[i]) * alpha):02x}" for i in range(3))


def debounced_redraw(canvas, delay=40):
    """Redraw once the canvas has stopped changing size, instead of on every pixel of a window drag."""
    state = {"job": None}

    def fire():
        state["job"] = None
        canvas.redraw()

    def on_configure(_):
        if state["job"] is not None:
            canvas.after_cancel(state["job"])
        state["job"] = canvas.after(delay, fire)
    canvas.bind("<Configure>", on_configure)


def hour_label(h):
    return datetime(2000, 1, 1, h).strftime("%I%p").lstrip("0").lower()


def time_ticks(t0, t1, max_ticks=6):
    """[(timestamp, label)] spread across [t0, t1]; clock times for short spans, dates for long ones."""
    if t1 <= t0:
        points = [t0]
    else:
        n = max(2, max_ticks)
        points = [t0 + (t1 - t0) * i / (n - 1) for i in range(n)]
    span = t1 - t0
    out = []
    for t in points:
        d = datetime.fromtimestamp(t)
        if span <= 86400:
            label = d.strftime("%I:%M%p").lstrip("0").lower()
        elif span <= 3 * 86400:
            label = d.strftime("%a %I%p").replace(" 0", " ").lower().capitalize()
        else:
            label = d.strftime("%b ") + str(d.day)
        out.append((t, label))
    return out


class HistoryChart(tk.Canvas):
    """Scores over time (top) and typical score by hour of day (bottom). Redraws itself on resize.

    theme: dict of hex colours (bg, panel, raised, line, text, muted, red, amber, green, grey, series)
    plus `font`, and `risky`/`go` score thresholds for the shaded bands.
    """

    def __init__(self, master, theme):
        super().__init__(master, bg=theme["bg"], highlightthickness=0)
        self.t = theme
        self.rows, self.profile = [], {}
        debounced_redraw(self)

    def set_data(self, rows, profile):
        """rows: [(ts, kind, score)] oldest first; profile: {hour: typical score}."""
        self.rows, self.profile = rows, profile
        self.redraw()

    # ---- drawing
    def _font(self, size, bold=False):
        return (self.t["font"], size, "bold") if bold else (self.t["font"], size)

    def _score_color(self, v):
        return self.t["green"] if v >= self.t["go"] else self.t["amber"] if v >= self.t["risky"] else self.t["red"]

    def _frame(self, x0, y0, x1, y1, title):
        """Title, a solid baseline, and dashed GO / RISKY threshold lines tinted with their zone colour."""
        t = self.t
        self.create_text(x0, y0 - 24, text=title.upper(), anchor="w", fill=t["muted"], font=self._font(8, True))
        for v, col in ((t["go"], t["green"]), (t["risky"], t["amber"]), (100, None)):
            y = y1 - v / 100 * (y1 - y0)
            self.aa.line([(x0, y), (x1, y)], blend(col, t["panel"], 0.45) if col else t["line"], dash=(2, 4))
            self.create_text(x0 - 8, y, text=str(v), anchor="e", fill=t["muted"], font=self._font(8))
        self.aa.line([(x0, y1), (x1, y1)], t["line"])
        self.create_text(x0 - 8, y1, text="0", anchor="e", fill=t["muted"], font=self._font(8))

    def _empty(self, x0, y0, x1, y1):
        self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="No checks yet. Run one from the Check tab.",
                         fill=self.t["muted"], font=self._font(11))

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.aa = AALayer(0, 0, w, h, self.t["bg"])
        if w >= 240 and h >= 240:
            self._draw(w, h)
        self.tag_lower(self.aa.paste(self))

    def _draw(self, w, h):
        k = max(1.0, self.winfo_fpixels("1i") / 96)  # keep pixel margins proportional on high-DPI screens
        left, right = 44 * k, w - 20 * k
        mid = h / 2
        top1, bot1 = 34 * k, mid - 34 * k
        top2, bot2 = mid + 34 * k, h - 30 * k
        self._frame(left, top1, right, bot1, "Scores over time")
        self._frame(left, top2, right, bot2, "Typical score by hour of day")
        if not self.rows:
            self._empty(left, top1, right, bot1)
            self._empty(left, top2, right, bot2)
            return
        self._scores_over_time(left, top1, right, bot1, k)
        self._by_hour(left, top2, right, bot2, k)

    def _scores_over_time(self, x0, y0, x1, y1, k):
        t = self.t
        t_min, t_max = self.rows[0][0], self.rows[-1][0]
        inset = 14 * k

        def px(ts):
            if t_max <= t_min:
                return (x0 + x1) / 2
            return x0 + inset + (ts - t_min) / (t_max - t_min) * (x1 - x0 - 2 * inset)

        def py(v):
            return y1 - max(0, min(100, v)) / 100 * (y1 - y0)

        pts = [(px(r[0]), py(r[2])) for r in self.rows if r[2] is not None]
        stamps = [r[0] for r in self.rows if r[2] is not None]
        segs, seg = [], []  # break the line across gaps of 6 h+, so days with no checks don't look measured
        for i, p in enumerate(pts):
            if seg and stamps[i] - stamps[i - 1] > 6 * 3600:
                segs.append(seg)
                seg = []
            seg.append(p)
        segs.append(seg)
        fill = blend(t["series"], t["panel"], 0.10)
        for sg in segs:
            if len(sg) > 1:
                curve = smooth(sg)
                self.aa.polygon([(sg[0][0], y1)] + curve + [(sg[-1][0], y1)], fill)
                self.aa.line(curve, t["series"], max(1.5, 1.5 * k))
        m = 2.5 * k
        for x, y in pts:
            self.aa.oval(x, y, m, t["panel"], t["series"], max(1, k))
        for ts, label in time_ticks(t_min, t_max):
            x = px(ts)
            self.aa.line([(x, y1), (x, y1 + 4 * k)], t["line"])
            self.create_text(x, y1 + 6 * k, text=label, anchor="n", fill=t["muted"], font=self._font(9))

    def _by_hour(self, x0, y0, x1, y1, k):
        t = self.t
        slot = (x1 - x0) / 24
        every = 1 if slot >= 34 * k else 2
        for hr in range(24):
            cx = x0 + (hr + 0.5) * slot
            if hr % every == 0:
                self.create_text(cx, y1 + 6 * k, text=hour_label(hr), anchor="n", fill=t["muted"],
                                 font=self._font(9))
            v = self.profile.get(hr)
            if v is None:
                continue
            top = y1 - max(0, min(100, v)) / 100 * (y1 - y0)
            w = max(2 * k, min(slot * 0.18, 6 * k))
            self.aa.rect(cx - w / 2, top, cx + w / 2, y1, self._score_color(v))
            self.create_text(cx, top - 3 * k, text=f"{v:.0f}", anchor="s", fill=t["muted"], font=self._font(8))


class MonitorChart(tk.Canvas):
    """Day-long monitor: ping over time with packet loss and problem periods marked (top), and the share of
    checks with problems for each hour of the day (bottom).

    theme: as HistoryChart, plus `router` (colour of the router line).
    """

    def __init__(self, master, theme):
        super().__init__(master, bg=theme["bg"], highlightthickness=0, height=420)
        self.t = theme
        self.samples, self.analysis = [], {}
        debounced_redraw(self)

    def set_data(self, samples, analysis):
        self.samples, self.analysis = samples, analysis or {}
        self.redraw()

    def _font(self, size, bold=False):
        return (self.t["font"], size, "bold") if bold else (self.t["font"], size)

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.aa = AALayer(0, 0, w, h, self.t["bg"])
        if w >= 240 and h >= 200:
            self._draw(w, h)
        self.tag_lower(self.aa.paste(self))

    def _draw(self, w, h):
        t = self.t
        k = max(1.0, self.winfo_fpixels("1i") / 96)
        left, right = 52 * k, w - 20 * k
        top1, bot1 = 34 * k, h * 0.58 - 26 * k
        top2, bot2 = h * 0.58 + 44 * k, h - 26 * k
        for y0, y1, title in ((top1, bot1, "Ping and packet loss over time"),
                              (top2, bot2, "Share of checks with problems, by hour of day")):
            self.create_text(left, y0 - 22 * k, text=title.upper(), anchor="w", fill=t["muted"], font=self._font(8, True))
            self.aa.line([(left, y1), (right, y1)], t["line"])
        if not self.samples:
            for y0, y1 in ((top1, bot1), (top2, bot2)):
                self.create_text((left + right) / 2, (y0 + y1) / 2, fill=t["muted"], font=self._font(11),
                                 text="Start a monitor to see how your connection behaves over the day.")
            return
        self._timeline(left, top1, right, bot1, k)
        self._hours(left, top2, right, bot2, k)

    def _timeline(self, x0, y0, x1, y1, k):
        t, a, ss = self.t, self.analysis, self.samples
        t_min = ss[0]["ts"]
        t_max = max(a.get("end", ss[-1]["ts"]), t_min + 60)
        inset = 6 * k

        def px(ts):
            return x0 + inset + (ts - t_min) / (t_max - t_min) * (x1 - x0 - 2 * inset)

        lat = [latency(s) for s in ss]
        peak = max([v for v in lat if v is not None] + [s.get("router_ms") or 0 for s in ss] + [40])
        ceiling = next((c for c in (50, 80, 120, 200, 300, 500, 1000, 3000) if c >= peak * 1.1), peak * 1.1)

        def py(v):
            return y1 - min(v, ceiling) / ceiling * (y1 - y0 - 8 * k)

        # problem periods and recording gaps, behind everything
        for inc in a.get("incidents", []):
            self.aa.rect(px(inc["start"]), y0, max(px(inc["end"]), px(inc["start"]) + 2), y1,
                         blend(t["red"], t["panel"], 0.22))
        for g0, g1 in a.get("gaps", []):
            self.aa.rect(px(g0), y0, px(g1), y1, blend(t["muted"], t["panel"], 0.10))
            self.create_text((px(g0) + px(g1)) / 2, y0 + 10 * k, text="not recorded", fill=t["muted"],
                             font=self._font(8))
        for v in (0, ceiling / 2, ceiling):
            y = py(v)
            self.aa.line([(x0, y), (x1, y)], t["line"])
            self.create_text(x0 - 8 * k, y, text=f"{v:.0f} ms", anchor="e", fill=t["muted"], font=self._font(9))
        # packet loss: red ticks rising from the floor, full height = 10%+
        for s in ss:
            lv = loss(s) or 0
            if lv > 0:
                x = px(s["ts"])
                self.aa.line([(x, y1), (x, y1 - min(lv, 10) / 10 * (y1 - y0) * 0.5)], t["red"], max(2, k))
        for series, color in (([s.get("router_ms") for s in ss], t["router"]), (lat, t["series"])):
            seg, prev_ts = [], None
            for s, v in zip(ss, series):
                if v is None or (prev_ts and s["ts"] - prev_ts > 180):
                    if len(seg) > 1:
                        self.aa.line(smooth(seg), color, max(1, 1.5 * k))
                    seg = []
                if v is not None:
                    seg.append((px(s["ts"]), py(v)))
                prev_ts = s["ts"]
            if len(seg) > 1:
                self.aa.line(smooth(seg), color, max(1, 1.5 * k))
        for ts, label in time_ticks(t_min, t_max, max_ticks=7):
            x = px(ts)
            self.aa.line([(x, y1), (x, y1 + 4 * k)], t["line"])
            self.create_text(x, y1 + 6 * k, text=label, anchor="n", fill=t["muted"], font=self._font(9))
        lx = x1 - 8 * k
        for label, color in (("Problem period", blend(t["red"], t["panel"], 0.5)), ("Packet loss", t["red"]),
                             ("Your router", t["router"]), ("Game servers", t["series"])):
            item = self.create_text(lx, y0 - 22 * k, text=label, anchor="e", fill=t["muted"], font=self._font(9))
            bx = self.bbox(item)[0]
            self.aa.rect(bx - 14 * k, y0 - 26 * k, bx - 5 * k, y0 - 18 * k, color)
            lx = bx - 24 * k

    def _hours(self, x0, y0, x1, y1, k):
        t, hours = self.t, self.analysis.get("hours", {})
        slot = (x1 - x0) / 24
        every = 1 if slot >= 34 * k else 2
        for v in (0, 50, 100):
            y = y1 - v / 100 * (y1 - y0 - 14 * k)
            self.aa.line([(x0, y), (x1, y)], t["line"])
            self.create_text(x0 - 8 * k, y, text=f"{v}%", anchor="e", fill=t["muted"], font=self._font(9))
        for hr in range(24):
            cx = x0 + (hr + 0.5) * slot
            if hr % every == 0:
                self.create_text(cx, y1 + 6 * k, text=hour_label(hr), anchor="n", fill=t["muted"], font=self._font(8))
            v = hours.get(hr)
            if v is None:
                continue
            pct = v["bad_pct"]
            color = t["green"] if pct < 5 else t["amber"] if pct < 15 else t["red"]
            top = y1 - max(pct, 1.5) / 100 * (y1 - y0 - 14 * k)  # a sliver even at 0% so covered hours show
            w = max(2 * k, min(slot * 0.18, 6 * k))
            self.aa.rect(cx - w / 2, top, cx + w / 2, y1, color)
            self.create_text(cx, top - 3 * k, text=f"{pct:.0f}", anchor="s", fill=t["muted"], font=self._font(8))
