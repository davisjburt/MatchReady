"""Desktop UI on plain Tk widgets (see DESIGN.md).

Layout: a slim nav rail, the current view as flat cards and table strips on the page background, and a
summary rail on the right that always holds the score and the Run check button.

Why not CustomTkinter: every CTk widget draws itself on its own canvas. With ~900 widgets, each resize step
redrew hundreds of rounded rectangles and the CTk scrollbar forced an extra full layout pass, so a resize step
took 250-600 ms. Plain frames and labels relayout in a few milliseconds.
"""
import ctypes
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageDraw, ImageTk
from tkinter import messagebox
import traceback
from datetime import datetime
from pathlib import Path

from . import __version__, monitor, predict, runner, scoring  # noqa: E402
from .aa import AALayer, smooth  # noqa: E402
from .charts import HistoryChart, MonitorChart, blend, debounced_redraw  # noqa: E402
from .gauge import Gauge  # noqa: E402
from . import release_update  # noqa: E402
from .updater import Updater, swap_and_restart  # noqa: E402
from .storage import History, db_path  # noqa: E402


# ---------- design tokens (sampled from the reference; contrast checked in DESIGN.md) ----------
SIDE, BG, CARD, RAIL = "#131a21", "#19212a", "#1f2731", "#1f2731"
TILE, TRACK, LINE = "#28323d", "#35404b", "#2a333e"
BTN2, BTN2_HOVER = "#35414d", "#3f4c59"
BTN, BTN_HOVER, BTN_OFF = "#1679c7", "#1f88dc", "#1d3a55"
TEXT, MUTED, LABEL = "#e7ebef", "#9aa3ad", "#8a94a0"
ACCENT = "#2ea3f7"            # active nav, live state, game-server series
ACCENT_2 = "#9fd4ff"          # second series (upload, UDP)
GREEN, AMBER, RED, GREY = "#3ec4a7", "#eab83a", "#ef5f76", "#6b7580"
INFO_BG, ERR_BG = "#16324c", "#45202b"
INK = "#0b1118"
DIM = LABEL
SERVER_LINE = ACCENT
VERDICT_COLOR = {"go": GREEN, "risky": AMBER, "bad": RED}
FONT = "Segoe UI"
SEMI = "Segoe UI Semibold"
NUM = "Segoe UI"                 # numbers; upgraded to Segoe UI Variable Display when installed
ICON = "Segoe MDL2 Assets"       # glyphs; upgraded to Segoe Fluent Icons when installed
WATCH_INTERVAL_S = 120
ALERT_BELOW = scoring.RISKY
RUN_LABEL = "Run check"
STALE_AFTER_S = 10 * 60
SETTLED_SPREAD = 5               # a live prediction only states a verdict once its ± range is this tight
WIDE = 860                       # Check view width (logical px) above which the side cards sit beside the score

PROFILE = scoring.PROFILE
K = 1.0                          # logical px -> physical px; set from the screen DPI at startup


def px(n):
    return int(round(n * K))


def font(size, weight=None, family=None):
    f = (family or FONT, -max(1, px(size)))
    return f + (weight,) if weight else f


def pick_fonts(root):
    global NUM, ICON
    fams = set(tkfont.families(root))
    if "Segoe UI Variable Display" in fams:
        NUM = "Segoe UI Variable Display"
    if "Segoe Fluent Icons" in fams:
        ICON = "Segoe Fluent Icons"


def region():
    try:
        return runner.load_config()["region"]
    except Exception:
        return "US Central"


TRACE_STYLE = {"servers": (SERVER_LINE, "Game server"), "router": (LABEL, "Router"), "load": (AMBER, "Under load")}
GRAPH_SCALES = (40, 60, 80, 100, 150, 200, 300, 500, 1000, 5000)


def color_for(sub):
    if sub is None:
        return GREY
    return GREEN if sub >= 80 else AMBER if sub >= 60 else RED


def word_for(sub):
    """Status in words, so colour is never the only signal."""
    if sub is None:
        return ""
    return "Good" if sub >= 80 else "Risky" if sub >= 60 else "Bad"


def fmt(v, unit="", digits=0):
    return "—" if v is None else f"{v:.{digits}f}{unit}"


def age_text(seconds):
    m = int(seconds // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60} h ago"


def delta(cur, prev, lower_better=True, digits=0):
    """(text, colour) for the change since the previous check: ▲/▼ plus size, green when it got better."""
    if cur is None or prev is None:
        return "", LABEL
    d = cur - prev
    if abs(d) < (0.5 if digits == 0 else 0.05):
        return "same", LABEL
    better = (d < 0) == lower_better
    return f"{'▲' if d > 0 else '▼'} {abs(d):.{digits}f}", GREEN if better else RED


# ---------- primitives ----------
class Text(tk.Label):
    """A flat label. configure() also accepts text_color= (kept from the CustomTkinter version)."""

    def __init__(self, master, text="", size=12, color=MUTED, bg=None, weight=None, family=None, **kw):
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        kw.setdefault("padx", 0)
        kw.setdefault("pady", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("highlightthickness", 0)
        super().__init__(master, text=text, font=font(size, weight, family), fg=color,
                         bg=bg or master.cget("bg"), **kw)

    def configure(self, cnf=None, **kw):
        if "text_color" in kw:
            kw["fg"] = kw.pop("text_color")
        return super().configure(cnf, **kw)

    config = configure


def caps(master, text, color=LABEL, **kw):
    """Small uppercase label: names a value, never carries a sentence."""
    return Text(master, text.upper(), 10, color, weight="bold", **kw)


def box(master, bg=None, **kw):
    return tk.Frame(master, bg=bg or master.cget("bg"), bd=0, highlightthickness=0, **kw)


def rule(master, color=LINE):
    return tk.Frame(master, bg=color, height=1, bd=0, highlightthickness=0)


def autowrap(label, pad=0, ref=None):
    """Wrap a label to its container's width, once the size settles (not on every pixel of a drag)."""
    ref = ref or label.master
    state = {"job": None}

    def apply():
        state["job"] = None
        if not label.winfo_exists():
            return
        w = max(px(120), ref.winfo_width() - px(pad))
        if abs(int(label.cget("wraplength") or 0) - w) >= px(6):
            label.configure(wraplength=w)

    def on_configure(e):
        if e.widget is ref:
            if state["job"] is not None:
                ref.after_cancel(state["job"])
            state["job"] = ref.after(60, apply)
    ref.bind("<Configure>", on_configure, add="+")
    return label


class Button(tk.Frame):
    """Flat button: primary (blue), secondary (slate) or ghost. Keyboard: Tab to focus, Enter/Space to press."""
    KINDS = {"primary": (BTN, BTN_HOVER, "#ffffff"), "secondary": (BTN2, BTN2_HOVER, TEXT),
             "ghost": (CARD, TILE, TEXT)}

    def __init__(self, master, text, command=None, kind="primary", icon=None, height=34, width=None, size=12):
        self.parent_bg = master.cget("bg")
        super().__init__(master, bd=0, height=px(height), width=px(width) if width else 1, takefocus=1,
                         cursor="hand2", highlightthickness=max(1, px(1)), highlightcolor=TEXT,
                         highlightbackground=self.parent_bg)
        self.pack_propagate(False)
        self.command, self.kind, self.enabled, self.hover = command, kind, True, False
        self.icon_color = None
        inner = tk.Frame(self, bd=0, highlightthickness=0)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        self.inner = inner
        self.icon = tk.Label(inner, text=icon or "", font=(ICON, -px(size + 1)), bd=0, padx=0, pady=0)
        if icon:
            self.icon.pack(side="left", padx=(0, px(8)))
        self.label = tk.Label(inner, text=text, font=font(size, "bold"), bd=0, padx=0, pady=0)
        self.label.pack(side="left")
        for w in (self, inner, self.icon, self.label):
            w.bind("<Button-1>", lambda e: self.invoke())
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
        for key in ("<Return>", "<space>"):
            self.bind(key, lambda e: self.invoke())
        self._paint()

    def _hover(self, on):
        self.hover = on
        self._paint()

    def _paint(self):
        bg, hover_bg, fg = self.KINDS[self.kind]
        if not self.enabled:
            bg, fg = (BTN_OFF, MUTED) if self.kind == "primary" else (BTN2 if self.kind != "ghost" else CARD, LABEL)
        elif self.hover:
            bg = hover_bg
        tk.Frame.configure(self, bg=bg, cursor="hand2" if self.enabled else "arrow")
        for w in (self.inner, self.icon, self.label):
            w.configure(bg=bg)
        self.label.configure(fg=fg)
        self.icon.configure(fg=self.icon_color or fg)

    def invoke(self):
        if self.enabled and self.command:
            self.command()

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self.label.configure(text=kw.pop("text"))
        if "command" in kw:
            self.command = kw.pop("command")
        if "kind" in kw:
            self.kind = kw.pop("kind")
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
        if "icon" in kw:
            self.icon.configure(text=kw.pop("icon"))
        if cnf or kw:
            tk.Frame.configure(self, cnf, **kw)
        self._paint()

    config = configure

    def cget(self, key):
        if key == "text":
            return self.label.cget("text")
        if key == "state":
            return "normal" if self.enabled else "disabled"
        return tk.Frame.cget(self, key)


class Toggle(Button):
    """A secondary button that switches something on and off; the dot turns green when it's on."""

    def __init__(self, master, text, command):
        self.name, self.on, self.user_cmd = text, False, command
        super().__init__(master, f"{text}: off", command=self._flip, kind="secondary", icon="●", size=12)
        self.icon.configure(font=font(10))
        self._sync()

    def _flip(self):
        self.on = not self.on
        self._sync()
        self.user_cmd()

    def _sync(self):
        self.icon_color = GREEN if self.on else LABEL
        self.label.configure(text=f"{self.name}: {'on' if self.on else 'off'}")
        self._paint()

    def get(self):
        return self.on

    def select(self):
        self.on = True
        self._sync()

    def deselect(self):
        self.on = False
        self._sync()


class Segmented(tk.Frame):
    """A row of mutually exclusive options; the chosen one is filled blue."""

    def __init__(self, master, values, value, command=None):
        super().__init__(master, bg=TRACK, bd=0, highlightthickness=0)
        self.value, self.command, self.enabled = value, command, True
        self.items = {}
        for v in values:
            lab = Text(self, v, 12, TEXT, bg=TRACK, weight="bold", padx=px(14), pady=px(7), cursor="hand2",
                       takefocus=1, highlightthickness=max(1, px(1)), highlightcolor=TEXT,
                       highlightbackground=TRACK)
            lab.pack(side="left")
            lab.bind("<Button-1>", lambda e, v=v: self._pick(v))
            for key in ("<Return>", "<space>"):
                lab.bind(key, lambda e, v=v: self._pick(v))
            self.items[v] = lab
        self._paint()

    def _pick(self, v):
        if self.enabled and v != self.value:
            self.value = v
            self._paint()
            if self.command:
                self.command(v)

    def _paint(self):
        for v, lab in self.items.items():
            sel = v == self.value
            bg = BTN if sel else TRACK
            fg = ("#ffffff" if sel else TEXT) if self.enabled else (MUTED if sel else LABEL)
            lab.configure(bg=bg if self.enabled or not sel else BTN_OFF, fg=fg, highlightbackground=bg)

    def get(self):
        return self.value

    def set(self, v):
        self.value = v
        self._paint()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
            self._paint()
        if cnf or kw:
            tk.Frame.configure(self, cnf, **kw)

    config = configure


class Dropdown(tk.Frame):
    """Shows the current choice; click (or Enter) opens a menu of the others."""

    def __init__(self, master, command, width=260):
        super().__init__(master, bg=TRACK, bd=0, width=px(width), height=px(28), takefocus=1, cursor="hand2",
                         highlightthickness=max(1, px(1)), highlightcolor=TEXT, highlightbackground=TRACK)
        self.pack_propagate(False)
        self.command, self.values, self.value = command, [], ""
        self.text = Text(self, "", 12, TEXT, bg=TRACK)
        self.text.pack(side="left", padx=px(10))
        self.arrow = Text(self, "", 10, MUTED, bg=TRACK, family=ICON)
        self.arrow.pack(side="right", padx=px(10))
        for w in (self, self.text, self.arrow):
            w.bind("<Button-1>", lambda e: self.open())
        for key in ("<Return>", "<space>"):
            self.bind(key, lambda e: self.open())

    def open(self):
        if not self.values:
            return
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT, activebackground=TILE, activeforeground=TEXT, bd=0,
                       font=font(12))
        for v in self.values:
            menu.add_command(label=v, command=lambda v=v: self._pick(v))
        menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())

    def _pick(self, v):
        self.set(v)
        if self.command:
            self.command(v)

    def set(self, v):
        self.value = v
        self.text.configure(text=v)

    def get(self):
        return self.value

    def configure(self, cnf=None, **kw):
        if "values" in kw:
            self.values = list(kw.pop("values"))
        if cnf or kw:
            tk.Frame.configure(self, cnf, **kw)

    config = configure


class Card(tk.Frame):
    """Flat panel: an uppercase label top-left, an optional meta value top-right, content in .body."""

    def __init__(self, master, title=None, meta="", pad=18):
        super().__init__(master, bg=CARD, bd=0, highlightthickness=0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.head = box(self, CARD)
        if title:
            self.head.grid(row=0, column=0, sticky="we", padx=px(pad), pady=(px(14), 0))
            caps(self.head, title).pack(side="left")
        self.meta = Text(self.head, meta, 11, MUTED)
        self.meta.pack(side="right")
        self.body = box(self, CARD)
        self.body.grid(row=1, column=0, sticky="nsew", padx=px(pad), pady=(px(12) if title else px(pad), px(pad)))
        self.body.columnconfigure(0, weight=1)


class Tooltip:
    """Shows text_fn() near the pointer after a short hover."""

    def __init__(self, widgets, text_fn):
        self.text_fn, self.tip, self.job, self.owner = text_fn, None, None, None
        for w in widgets:
            w.bind("<Enter>", self._schedule, add="+")
            w.bind("<Leave>", self._hide, add="+")

    def _schedule(self, e):
        self._hide()
        self.owner = e.widget
        self.job = e.widget.after(450, lambda: self._show(e.widget))

    def _show(self, w):
        self.job = None
        text = self.text_fn()
        if not text:
            return
        self.tip = tk.Toplevel(w)
        self.tip.overrideredirect(True)
        self.tip.configure(bg=LINE)
        Text(self.tip, text, 11, TEXT, bg=TILE, padx=px(10), pady=px(6)).pack(padx=1, pady=1)
        x, y = w.winfo_pointerx() + px(12), w.winfo_pointery() + px(14)
        self.tip.geometry(f"+{x}+{y}")

    def _hide(self, _=None):
        if self.job is not None and self.owner is not None:
            try:
                self.owner.after_cancel(self.job)
            except tk.TclError:
                pass
        self.job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


# ---------- canvas drawing (the Check view is one canvas: see CheckPage) ----------
class LiveTrace(tk.Canvas):
    """Ping during the check: one line per target with a soft fill, red ticks for lost packets, and the
    latest value marked on the game-server line.

    Pings arrive every 0.1-0.15 s but snapshots only ~4x/s, so drawing each snapshot as-is makes the line jump
    1-2 points at a time. Instead the line's head glides: each series has a revealed position (a fractional
    point count) that moves at the measured ping rate, trailing the data by about one snapshot so it never
    has to stop and wait. The y scale eases between sizes too. Only this small canvas redraws per frame."""

    FRAME_MS = 16
    LAG_S = 0.3          # how far the head trails the newest data while data is still arriving
    SCALE_TAU = 0.25     # y-scale easing time constant (s)

    def __init__(self, master):
        super().__init__(master, bg=CARD, bd=0, highlightthickness=0)
        self.traces, self.counts = {}, {}
        self.shown = {}                    # key -> revealed point count (float, in global indices)
        self.rate = {}                     # key -> (t0, count0) for measuring the ping rate
        self.last_data = 0.0
        self.top = None                    # y-scale top as drawn (eased)
        self.job, self.t_prev = None, None
        self.bind("<Configure>", lambda e: self._render())

    def set(self, traces, counts=None):
        traces = traces or {}
        counts = counts or {k: len(v) for k, v in traces.items()}
        now = time.perf_counter()
        if set(traces) != set(self.traces):  # new phase (e.g. idle -> downloading): start from the left
            self.shown, self.rate, self.top = {}, {}, None
        for k, total in counts.items():
            if total < self.counts.get(k, 0):  # a new check started
                self.shown.pop(k, None)
                self.rate.pop(k, None)
            self.shown.setdefault(k, 0.0)
            self.rate.setdefault(k, (now, total))
        if counts != self.counts:
            self.last_data = now
        self.traces, self.counts = traces, counts
        if not traces:
            self.shown, self.rate, self.top = {}, {}, None
            self._render()
            return
        self._kick()

    # ---- animation
    def _kick(self):
        if self.job is None:
            self.t_prev = time.perf_counter()
            self.job = self.after(self.FRAME_MS, self._tick)

    def _ping_rate(self, k, now):
        t0, c0 = self.rate[k]
        if now - t0 > 0.6 and self.counts[k] > c0:
            return (self.counts[k] - c0) / (now - t0)
        return 1 / 0.15

    def _tick(self):
        self.job = None
        now = time.perf_counter()
        dt = min(0.1, now - self.t_prev)
        self.t_prev = now
        busy = False
        since = now - self.last_data
        live = since < 0.5  # data still arriving: trail it; otherwise catch up fully
        for k, total in self.counts.items():
            s, rate = self.shown.get(k, 0.0), self._ping_rate(k, now)
            # Where the data is now, predicted from the ping rate (snapshots land in steps; this glides),
            # minus the lag. Never past what has actually arrived.
            target = min(total, max(0.0, total + rate * (since - self.LAG_S))) if live else total
            v = rate + 1.5 * (target - s)  # steady speed, gently nudged to hold the lag
            s = min(float(total), s + max(0.0, v) * dt)
            if not live and total - s < 0.02:
                s = float(total)
            self.shown[k] = s
            busy |= s < total
        want = self._want_top()
        if self.top is None:
            self.top = want
        else:
            self.top += (want - self.top) * (1 - math.exp(-dt / self.SCALE_TAU))
            if abs(want - self.top) < 0.3:
                self.top = want
            busy |= self.top != want
        self._render()
        if busy or live:
            self.job = self.after(self.FRAME_MS, self._tick)

    def _visible(self, k):
        """[(global index, value)] for the revealed part of series k, plus the fractional head point."""
        series, total = self.traces.get(k, []), self.counts.get(k, 0)
        base, s = total - len(series), self.shown.get(k, 0.0)
        out = [(base + i, v) for i, v in enumerate(series) if base + i <= s - 1 + 1e-9]
        head = s - 1
        i0 = int(math.floor(head)) - base
        if 0 <= i0 < len(series) - 1 and head > base + i0 + 1e-9:
            a, b = series[i0], series[i0 + 1]
            if a is not None and b is not None:
                f = head - (base + i0)
                out.append((head, a + (b - a) * f))
        return out

    def _want_top(self):
        vals = [v for k in self.traces for _, v in self._visible(k) if v is not None]
        want = max(40.0, max(vals) * 1.15) if vals else 40.0
        return float(next((s for s in GRAPH_SCALES if s >= want), want))

    # ---- drawing
    def _render(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 40:
            return
        left, top_pad, bottom, right = px(30), px(20), h - px(4), w - px(4)
        span = bottom - top_pad
        if not self.traces:
            for frac in (0.0, 0.5, 1.0):
                y = bottom - frac * span
                self.create_line(left, y, right, y, fill=LINE, dash=() if frac == 0 else (2, 4))
            self.create_text((left + right) / 2, (top_pad + bottom) / 2, fill=LABEL, font=font(11),
                             text="Every ping draws here while a check runs.")
            return
        top = self.top or self._want_top()
        aa = AALayer(0, 0, w, h, CARD)
        n = runner.TRACE_POINTS
        dx = (right - left) / (n - 1)
        off = max([0.0] + [s - n for s in self.shown.values()])  # scroll once a series outgrows the window

        def xy(g, v):
            return left + (g - off) * dx, bottom - min(v, top) / top * span

        for frac in (0.0, 0.5, 1.0):
            y = round(bottom - frac * span) + 0.5  # on a pixel centre, so the 1 px grid stays crisp
            aa.line([(left, y), (right, y)], LINE, 1, dash=None if frac == 0 else (2, 4))
        legend_x, lost, tag = left, False, None
        legend = []
        for key in self.traces:
            color, label = TRACE_STYLE[key]
            pts = self._visible(key)
            segs, seg = [], []
            for g, v in pts:
                if v is None:  # lost packet: red tick on the baseline, and the line breaks
                    lost = True
                    x = xy(g, 0)[0]
                    aa.line([(x, bottom - px(8)), (x, bottom)], RED, max(1, px(2)))
                    segs.append(seg)
                    seg = []
                else:
                    seg.append(xy(g, v))
            segs.append(seg)
            for sg in segs:
                if len(sg) > 1:
                    curve = smooth(sg)
                    if key != "router":
                        aa.polygon([(sg[0][0], bottom)] + curve + [(sg[-1][0], bottom)], blend(color, CARD, 0.10))
                    aa.line(curve, color, max(1, px(1.5)))
            last = next(((g, v) for g, v in reversed(pts) if v is not None), None)
            if key != "router" and last is not None:
                tag = xy(*last)
            legend.append((color, f"● {label}" + (f" {last[1]:.0f} ms" if last is not None else "")))
        if tag:
            aa.oval(*tag, px(3.5), CARD, TEXT, max(1, px(1.5)))
        aa.paste(self)  # first, so the text sits on top
        for frac in (0.0, 0.5, 1.0):
            self.create_text(left - px(6), bottom - frac * span, text=f"{top * frac:.0f}", anchor="e", fill=LABEL,
                             font=font(9))
        for color, text in legend:
            item = self.create_text(legend_x, px(6), anchor="w", fill=color, font=font(10, "bold"), text=text)
            legend_x = self.bbox(item)[2] + px(14)
        if lost:
            self.create_text(legend_x, px(6), anchor="w", fill=RED, font=font(10, "bold"), text="| Lost packet")


def _hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def donut_image(master, size, outer, inner, bg=CARD):
    """The two rings as an anti-aliased image (Tk canvas arcs have jagged edges): drawn at 4x, then scaled
    down. Cached per value, since the same few images are shown again and again."""
    cache = master.__dict__.setdefault("_donut_cache", {})
    key = (size, None if outer is None else round(outer, 1), None if inner is None else round(inner, 1), bg)
    if key not in cache:
        ss = 4
        img = Image.new("RGB", (size * ss, size * ss), _hex_rgb(bg))
        d = ImageDraw.Draw(img)
        w = max(2, px(4)) * ss
        for i, (v, col) in enumerate(zip((outer, inner), (ACCENT, ACCENT_2))):
            r = size * ss / 2 - w / 2 - ss - i * px(11) * ss
            c = size * ss / 2
            bb = (c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2)
            d.ellipse(bb, outline=_hex_rgb(TRACK), width=w)
            if v is not None and v > 0:
                d.arc(bb, -90, -90 + 360 * max(0.0, min(100.0, v)) / 100, fill=_hex_rgb(col), width=w)
        if len(cache) > 32:
            cache.clear()
        cache[key] = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS), master=master)
    return cache[key]


def draw_donut(c, cx, cy, size, outer, inner, tags=("drawn",)):
    """Two thin concentric rings: packets delivered to the game servers (outer) and over UDP (inner)."""
    c.create_image(cx, cy, image=donut_image(c, int(size), outer, inner), tags=tags)
    c.create_text(cx, cy - px(5), text="—" if outer is None else f"{outer:.1f}%", fill=TEXT,
                  font=font(15, family=NUM), tags=tags)
    c.create_text(cx, cy + px(13), text="delivered", fill=LABEL, font=font(9), tags=tags)


def draw_speed(c, x0, y0, w, h, down, up, have_down, have_up, tags=("drawn",)):
    """Download and upload as two thin bars on a shared scale (games need well under 1 Mbps)."""
    left, top, bottom, right = x0 + px(30), y0 + px(16), y0 + h - px(18), x0 + w
    nums = [v for v in (down, up) if v]
    peak = max(nums + [10.0])
    scale = next((s for s in (10, 25, 50, 100, 250, 500, 1000, 2500, 10000) if s >= peak), peak)
    for frac in (0.0, 0.5, 1.0):
        y = bottom - frac * (bottom - top)
        c.create_line(left, y, right, y, fill=LINE, dash=() if frac == 0 else (2, 4), tags=tags)
        c.create_text(left - px(6), y, text=f"{scale * frac:.0f}", anchor="e", fill=LABEL, font=font(9), tags=tags)
    slots = (right - left) / 2
    for i, (label, col, v, have) in enumerate((("Down", ACCENT, down, have_down), ("Up", ACCENT_2, up, have_up))):
        cx = left + slots * (i + 0.5)
        c.create_text(cx, bottom + px(9), text=label, fill=LABEL, font=font(10), tags=tags)
        if v:
            y = bottom - min(v, scale) / scale * (bottom - top)
            bw = max(2, px(4))
            c.create_rectangle(cx - bw / 2, y, cx + bw / 2, bottom, fill=col, outline="", tags=tags)
            c.create_text(cx, y - px(8), text=f"{v:.0f}", fill=TEXT, font=font(10, "bold"), tags=tags)
        else:
            c.create_text(cx, bottom - px(10), text="n/a" if have else "—", fill=LABEL, font=font(10), tags=tags)


def draw_bar(c, x0, y, x1, h, frac, color, tags=("drawn",)):
    c.create_rectangle(x0, y, x1, y + h, fill=TRACK, outline="", tags=tags)
    if frac > 0:
        c.create_rectangle(x0, y, x0 + (x1 - x0) * frac, y + h, fill=color, outline="", tags=tags)


def draw_spark(c, x0, y0, w, h, scores, bg, tags=("drawn",)):
    """Recent scores: a filled line with the GO/RISKY thresholds and the latest value tagged."""
    left, right, top, bottom = x0 + px(2), x0 + w - px(20), y0 + px(24), y0 + h - px(4)

    def py(v):
        return bottom - max(0, min(100, v)) / 100 * (bottom - top)
    mark = c.create_line(0, 0, 0, 0, tags=tags)  # the anti-aliased layer goes here, under the grid and text
    for v, col in ((scoring.GO, GREEN), (scoring.RISKY, AMBER)):
        c.create_line(left, py(v), right, py(v), fill=blend(col, bg, 0.4), dash=(2, 3), tags=tags)
        c.create_text(x0 + w - px(1), py(v), text=str(v), anchor="e", fill=LABEL, font=font(9), tags=tags)
    if len(scores) < 2:
        c.create_text((left + right) / 2, (top + bottom) / 2 + px(6), fill=LABEL, font=font(10), tags=tags,
                      text="Your checks chart here.")
        c.delete(mark)
        return
    n = len(scores)
    pts = [(left + i * (right - left - px(8)) / (n - 1), py(v)) for i, v in enumerate(scores)]
    aa = AALayer(x0, y0, w, h, bg)
    curve = smooth(pts)
    aa.polygon([(left, bottom)] + curve + [(pts[-1][0], bottom)], blend(AMBER, bg, 0.10))
    aa.line(curve, AMBER, max(1, px(1.5)))
    x, y = pts[-1]
    aa.oval(x, y, px(3.5), bg, TEXT, max(1, px(1.5)))
    tx = min(max(x, left + px(14)), right - px(4))
    ty = y - px(15) if y - px(15) > y0 + px(8) else y + px(15)
    c.create_rectangle(tx - px(12), ty - px(8), tx + px(12), ty + px(8), fill=TEXT, outline="", tags=tags)
    c.create_text(tx, ty, text=str(scores[-1]), fill=INK, font=font(10, "bold"), tags=tags)
    c.tag_lower(aa.paste(c, tags), mark)
    c.delete(mark)


class VText:
    """A piece of text drawn by a canvas page. Mirrors the bits of the Label API the app uses."""

    def __init__(self, page, text="", fg=MUTED, font_=None):
        self.page, self.text, self.fg, self.font, self.visible = page, text, fg, font_, True

    def configure(self, cnf=None, **kw):
        if "text_color" in kw:
            kw["fg"] = kw.pop("text_color")
        changed = False
        for k in ("text", "fg", "font"):
            if k in kw and getattr(self, k) != kw[k]:
                setattr(self, k, kw[k])
                changed = True
        if changed:
            self.page.request()

    config = configure

    def cget(self, key):
        return {"text": self.text, "fg": self.fg}.get(key)

    def grid(self, **_):
        if not self.visible:
            self.visible = True
            self.page.request()

    def grid_remove(self):
        if self.visible:
            self.visible = False
            self.page.request()


class VStat:
    """Label over a number with its unit and the change since the last check."""

    def __init__(self, page, label, unit):
        self.page, self.label, self.unit = page, label, unit
        self.value, self.delta = "—", ("", LABEL)

    def set(self, text, d=("", LABEL)):
        if (text, d) != (self.value, self.delta):
            self.value, self.delta = text, d
            self.page.request()


class VCell:
    """A measurement (table cell or monitor figure): value, detail and a 0-100 health score."""

    def __init__(self, page, label):
        self.page, self.label, self.data = page, label, (None, "", None)

    def set(self, value, detail="", sub=None):
        if self.data == (value, detail, sub):
            return  # live updates call this 4x/s; skip redraws when nothing changed
        self.data = (value, detail, sub)
        self.page.request()


class VBar:
    """A thin progress bar drawn by a canvas page."""

    def __init__(self, page, color=ACCENT):
        self.page, self.frac, self.col = page, 0.0, color

    def set(self, frac, color=None):
        frac = max(0.0, min(1.0, frac))
        if (frac, color or self.col) != (self.frac, self.col):
            self.frac, self.col = frac, color or self.col
            self.page.request()

    def color(self, c):
        self.set(self.frac, c)


class VSpark:
    def __init__(self, page):
        self.page, self.scores = page, []

    def set_data(self, scores):
        self.scores = list(scores[-30:])
        self.page.request()


class CanvasPage(tk.Frame):
    """A view drawn as items on one scrollable canvas; subclasses implement draw(width) -> content height.

    Why: on Windows every Tk label and frame is a native window, and resizing moves and repaints each one.
    With ~450 of them a resize step took 100-200 ms. Canvas items are not windows, so redrawing a whole view
    costs a few ms. Only interactive controls (buttons, the dial, charts) stay widgets, embedded with put()."""

    def __init__(self, master, bg=BG):
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self.bg = bg
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.c = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0, yscrollincrement=px(24))
        self.c.grid(row=0, column=0, sticky="nsew")
        self.bar = tk.Canvas(self, width=px(4), bg=bg, bd=0, highlightthickness=0)
        self.bar.grid(row=0, column=1, sticky="ns", padx=(px(2), px(3)))
        self.c.configure(yscrollcommand=self._thumb)
        self.bar.bind("<Button-1>", self._drag)
        self.bar.bind("<B1-Motion>", self._drag)
        self.c.bind("<Configure>", lambda e: self.render())
        self.view, self.pending, self.wins = (0.0, 1.0), False, {}

    def vt(self, text="", fg=MUTED, fnt=None):
        return VText(self, text, fg, fnt)

    def request(self):
        if not self.pending:
            self.pending = True
            self.after_idle(self.render)

    def render(self):
        self.pending = False
        W = self.c.winfo_width()
        if W < 60:
            return
        self.c.delete("drawn")
        h = self.draw(W)
        self.c.configure(scrollregion=(0, 0, W, h))

    def draw(self, W):
        return 0

    # ---- scrolling
    def _thumb(self, lo, hi):
        lo, hi = float(lo), float(hi)
        self.view = (lo, hi)
        self.bar.delete("all")
        if hi - lo < 0.999:
            h = self.bar.winfo_height()
            self.bar.create_rectangle(0, lo * h, px(4), hi * h, fill=TRACK, outline="")

    def _drag(self, e):
        h = max(1, self.bar.winfo_height())
        span = self.view[1] - self.view[0]
        self.c.yview_moveto(max(0.0, min(1.0 - span, e.y / h - span / 2)))

    def wheel(self, delta):
        if self.view[1] - self.view[0] < 0.999:
            self.c.yview_scroll(int(-delta / 120 * 3), "units")

    def yview_moveto(self, f):
        self.c.yview_moveto(f)

    # ---- drawing helpers
    def _t(self, x, y, text, size=12, color=TEXT, weight=None, family=None, anchor="nw", width=None, fnt=None,
           tags=()):
        kw = {"width": width} if width else {}
        return self.c.create_text(x, y, text=text, anchor=anchor, fill=color, tags=("drawn",) + tuple(tags),
                                  font=fnt or font(size, weight, family), **kw)

    def _b(self, item):
        return self.c.bbox(item)[3]

    def _r(self, item):
        return self.c.bbox(item)[2]

    def _card(self, x, y, w, h, title=None, meta=None):
        c, P = self.c, px(18)
        rect = c.create_rectangle(x, y, x + w, y + h, fill=CARD, outline="", tags="drawn")
        c.tag_lower(rect)
        if title:
            self._t(x + P, y + px(14), title.upper(), 10, LABEL, "bold")
        if meta is not None and meta.text:
            self._t(x + w - P, y + px(14), meta.text, 11, meta.fg, anchor="ne")

    def put(self, widget, x, y, anchor="nw", width=None, height=None):
        """Place an embedded widget (created with this page's canvas as its master)."""
        kw = {k: v for k, v in (("width", width), ("height", height)) if v}
        item = self.wins.get(widget)
        if item is None:
            self.wins[widget] = self.c.create_window(x, y, window=widget, anchor=anchor, **kw)
        else:
            self.c.coords(item, x, y)
            self.c.itemconfigure(item, anchor=anchor, **kw)


class CheckPage(CanvasPage):
    """The Check view: score card with the dial, packets and speed cards, advice strips, measurement table."""

    def __init__(self, master):
        super().__init__(master)
        self.gauge, self.gwin = None, None
        self.meta = self.vt("", ACCENT)
        self.verdict = self.vt("Not tested", GREY, font(22, family=SEMI))
        self.caps = self.vt("Run a check before you queue.", MUTED)
        self.feel = self.vt("", TEXT)
        self.feel.visible = False
        self.stats = {k: VStat(self, label, unit) for k, label, unit in (
            ("ping", "Ping", "ms"), ("jitter", "Jitter", "ms"), ("loss", "Packet loss", "%"),
            ("bloat", "Bufferbloat", "ms"))}
        self.trace_title = self.vt("", MUTED)
        self.trace_view = LiveTrace(self.c)  # animates itself; the page only positions it
        self.side = (None, None, None, None, False, False)
        self.advice = ([], "Advice shows up here after your first check.", AMBER)
        self.cells, self.groups = {}, ([], [])
        self.hints = [self.vt("", MUTED), self.vt("", MUTED)]
        self.tip, self.tip_job = None, None

    def cell(self, name, group):
        v = self.cells[name] = VCell(self, name)
        v.tag = f"tip{len(self.cells)}"
        self.groups[group].append(v)
        self.c.tag_bind(v.tag, "<Enter>", lambda e, v=v: self._tip_show(v))
        self.c.tag_bind(v.tag, "<Leave>", lambda e: self._tip_hide())
        return v

    def set_trace(self, title, traces, counts=None):
        self.trace_title.configure(text=title)  # re-lays out the page only when the title changes
        self.trace_view.set(traces, counts)

    def set_side(self, *vals):
        if vals != self.side:
            self.side = vals
            self.request()

    def set_advice(self, lines, empty=None, tone=AMBER):
        self.advice = (list(lines), empty, tone)
        self.request()

    # ---- tooltips on table cells
    def _tip_show(self, cell):
        self._tip_hide()
        self.tip_job = self.after(450, lambda: self._tip_open(cell))

    def _tip_open(self, cell):
        self.tip_job = None
        if not cell.data[1]:
            return
        self.tip = tk.Toplevel(self)
        self.tip.overrideredirect(True)
        self.tip.configure(bg=LINE)
        Text(self.tip, cell.data[1], 11, TEXT, bg=TILE, padx=px(10), pady=px(6)).pack(padx=1, pady=1)
        self.tip.geometry(f"+{self.winfo_pointerx() + px(12)}+{self.winfo_pointery() + px(14)}")

    def _tip_hide(self):
        if self.tip_job is not None:
            self.after_cancel(self.tip_job)
            self.tip_job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None

    # ---- layout
    def draw(self, W):
        g, L, R = px(12), px(2), W - px(18)
        wide = W / K >= WIDE
        side_w = px(270)
        score_w = (R - L - side_w - g) if wide else (R - L)
        h_score = self._score_card(L, 0, score_w)
        if wide:
            sx = L + score_w + g
            hp = self._packets_card(sx, 0, side_w, 0)
            hs = max(px(170), h_score - hp - g)
            self._speed_card(sx, hp + g, side_w, hs)
            y = max(h_score, hp + g + hs) + g
        else:
            y = h_score + g
            half = (R - L - g) / 2
            hp = self._packets_card(L, y, half, 0)
            self._speed_card(L + half + g, y, half, hp)
            y += hp + g
        y = self._advice_block(L, y + px(2), R)
        y = self._table(L, y + px(22), R)
        return y + px(22)

    def _score_card(self, x, y, w):
        c, P = self.c, px(18)
        top = y + px(44)
        gw, gh = self.gauge.winfo_reqwidth(), self.gauge.winfo_reqheight()
        self.put(self.gauge, x + P, top)
        ix = x + P + gw + px(20)
        iw = max(px(160), x + w - P - ix)
        iy = self._b(self._t(ix, top + px(6), "VERDICT", 10, LABEL, "bold")) + px(2)
        iy = self._b(self._t(ix, iy, self.verdict.text, color=self.verdict.fg, fnt=self.verdict.font))
        if self.caps.text:
            iy = self._b(self._t(ix, iy + px(4), self.caps.text, 12, self.caps.fg, width=iw))
        if self.feel.visible and self.feel.text:
            iy = self._b(self._t(ix, iy + px(4), self.feel.text, 12, self.feel.fg, width=iw))
        col = min(iw / 2, px(190))
        row_y, row_bottom = iy + px(18), iy
        for i, st in enumerate(self.stats.values()):
            if i == 2:
                row_y = row_bottom + px(12)
            sx = ix + (i % 2) * col
            ly = self._b(self._t(sx, row_y, st.label.upper(), 10, LABEL, "bold")) + px(4)
            v = self._t(sx, ly, st.value, 20, TEXT, family=NUM)
            vb = self._b(v)
            u = self._t(self._r(v) + px(4), vb - px(4), st.unit, 11, LABEL, anchor="sw")
            if st.delta[0]:
                self._t(self._r(u) + px(10), vb - px(4), st.delta[0], 11, st.delta[1], "bold", anchor="sw")
            row_bottom = max(row_bottom, vb)
        ry = max(top + gh, row_bottom) + px(14)
        c.create_line(x + P, ry, x + w - P, ry, fill=LINE, tags="drawn")
        hy = ry + px(12)
        hb = self._b(self._t(x + P, hy, "PING DURING THE CHECK", 10, LABEL, "bold"))
        if self.trace_title.text:
            self._t(x + w - P, hy, self.trace_title.text, 11, MUTED, anchor="ne")
        ty = hb + px(8)
        self.put(self.trace_view, x + P, ty, width=w - 2 * P, height=px(118))
        h = ty + px(118) + P - y
        self._card(x, y, w, h, "Connection score", self.meta)
        return h

    def _packets_card(self, x, y, w, min_h):
        P, size = px(18), px(112)
        g_loss, u_loss = self.side[0], self.side[1]
        cy = y + px(44) + size / 2
        draw_donut(self.c, x + w / 2, cy, size, None if g_loss is None else 100 - g_loss,
                   None if u_loss is None else 100 - u_loss)
        a = self._t(x + w / 2, cy + size / 2 + px(12), "● Game servers  " +
                    ("—" if g_loss is None else f"{g_loss:.1f}% lost"), 10, ACCENT, anchor="n")
        b = self._t(x + w / 2, self._b(a) + px(3), "● Game traffic (UDP)  " +
                    ("—" if u_loss is None else f"{u_loss:.1f}% lost"), 10, ACCENT_2, anchor="n")
        h = max(min_h, self._b(b) + P - y)
        self._card(x, y, w, h, "Packets delivered")
        return h

    def _speed_card(self, x, y, w, h):
        P = px(18)
        h = max(h, px(170))
        draw_speed(self.c, x + P, y + px(40), w - 2 * P, h - px(40) - P, *self.side[2:])
        self._card(x, y, w, h, "Speed · Mbps")
        return h

    def _advice_block(self, L, y, R):
        lines, empty, tone = self.advice
        y = self._b(self._t(L + px(2), y, "WHAT'S AFFECTING YOU", 10, LABEL, "bold")) + px(8)
        for line in lines or [empty or ""]:
            tx = L + (px(36) if lines else px(18))
            t = self._t(tx, y + px(12), line, 12, TEXT if lines else MUTED, width=R - tx - px(18))
            h = self._b(t) + px(12) - y
            rect = self.c.create_rectangle(L, y, R, y + h, fill=CARD, outline="", tags="drawn")
            self.c.tag_lower(rect)
            if lines:
                self.c.create_rectangle(L + px(18), y + px(19), L + px(24), y + px(25), fill=tone, outline="",
                                        tags="drawn")
            y += h + px(4)
        return y

    def _table(self, L, y, R):
        c, half = self.c, (R - L) / 2
        hb = y
        for i, title in enumerate(("PATH TO THE GAME", "YOUR SETUP")):
            hb = self._b(self._t(L + i * half + px(18), y, title, 10, LABEL, "bold"))
            hint = self.hints[i]
            if hint.text:
                self._t(L + (i + 1) * half - px(22), y, hint.text, 11, hint.fg, anchor="ne")
        y = hb + px(8)
        rh = px(44)
        for a, b in zip(*self.groups):
            c.create_rectangle(L, y, R, y + rh, fill=CARD, outline="", tags="drawn")
            mid = y + rh / 2
            for i, cell in enumerate((a, b)):
                x0, x1 = L + i * half + px(18), L + (i + 1) * half - px(18)
                value, _, score = cell.data
                col = color_for(score)
                word_x = x1 - px(48)
                bar_x1 = word_x - px(10)
                bar_x0 = bar_x1 - px(56)
                self._t(x0, mid, cell.label, 12, TEXT, anchor="w", tags=(cell.tag,))
                self._t(bar_x0 - px(12), mid, value or "—", 12, TEXT, anchor="e", tags=(cell.tag,))
                draw_bar(c, bar_x0, mid - px(1.5), bar_x1, px(3),
                         0 if score is None else max(0.04, min(1.0, score / 100)), col)
                if score is not None:
                    self._t(word_x, mid, word_for(score), 11, col, "bold", anchor="w")
            y += rh + px(4)
        return y


class RailPage(CanvasPage):
    """The summary rail: score and forecast, facts, Run check and Watch mode, progress, score history."""

    def __init__(self, master):
        super().__init__(master, RAIL)
        self.age = self.vt("Not tested yet", MUTED)
        self.score_val, self.score_delta = self.vt("—", TEXT), self.vt("", LABEL)
        self.fc_val, self.fc_delta = self.vt("—", TEXT), self.vt("", LABEL)
        self.server, self.server_note = self.vt("—", TEXT), self.vt("", MUTED)
        self.network = self.vt("—", TEXT)
        self.forecast = self.vt("Shows up after a check.", MUTED, font(11))
        self.status = self.vt("About 30 seconds. Close downloads and streams first.", MUTED)
        self.step_line, self.watch_status = self.vt("", LABEL), self.vt("", MUTED)
        self.progress, self.spark = VBar(self), VSpark(self)
        self.spark_label = self.vt("Your score over time", MUTED)
        self.run_btn = self.watch = None

    def draw(self, W):
        c, t, b = self.c, self._t, self._b
        x0, x1 = px(22), W - px(10)
        w = x1 - x0
        y = b(t(x0, px(24), "Ready to queue?", 16, TEXT))
        y = b(t(x0, y + px(2), self.age.text, 11, self.age.fg))
        y += px(22)
        bottom = y
        for i, (label, v, d) in enumerate((("SCORE", self.score_val, self.score_delta),
                                           ("NEXT 30 MIN", self.fc_val, self.fc_delta))):
            sx = x0 + i * w / 2
            ly = b(t(sx, y, label, 10, LABEL, "bold")) + px(4)
            vi = t(sx, ly, v.text, 24, v.fg, family=NUM)
            if d.text:
                t(self._r(vi) + px(8), b(vi) - px(5), d.text, 11, d.fg, "bold", anchor="sw")
            bottom = max(bottom, b(vi))
        y = bottom + px(20)
        c.create_line(x0, y, x1, y, fill=LINE, tags="drawn")
        y += px(4)
        for label, v, note in (("GAME SERVER", self.server, self.server_note), ("NETWORK", self.network, None),
                               ("FORECAST", self.forecast, None)):
            y = b(t(x0, y + px(14), label, 10, LABEL, "bold")) + px(3)
            y = b(t(x0, y, v.text, color=v.fg, fnt=v.font or font(12), width=w))
            if note is not None and note.text:
                y = b(t(x0, y + px(2), note.text, 11, note.fg, width=w))
        y += px(24)
        if self.run_btn is not None:
            self.put(self.run_btn, x0, y, width=w, height=px(36))
            y += px(36) + px(8)
            self.put(self.watch, x0, y, width=w, height=px(34))
            y += px(34) + px(14)
        draw_bar(c, x0, y, x1, max(1, px(2)), self.progress.frac, self.progress.col)
        y += px(8)
        for v in (self.status, self.step_line, self.watch_status):
            if v.text:
                y = b(t(x0, y + px(2), v.text, 11, v.fg, width=w))
        y += px(26)
        y = b(t(x0, y, "HISTORY", 10, LABEL, "bold")) + px(10)
        draw_spark(c, x0, y, w, px(96), self.spark.scores, RAIL)
        y += px(96) + px(8)
        c.create_rectangle(x0, y + px(7), x0 + px(10), y + px(7) + max(1, px(2)), fill=AMBER, outline="",
                           tags="drawn")
        y = b(t(x0 + px(18), y, self.spark_label.text, 11, MUTED))
        return y + px(22)


class MonitorPage(CanvasPage):
    """The Monitor view: controls, verdict, six figures, the day chart, findings and the maintenance request."""
    NAMES = ("Usable for games", "Problem periods", "Dropouts", "Packet loss", "Ping", "Worst hours")

    def __init__(self, master):
        super().__init__(master)
        self.progress = VBar(self)
        self.status = self.vt(f"A quick check every minute, plus a full check with a speed test every "
                              f"{monitor.FULL_EVERY_S // 3600} hours. Keeps this PC awake; if MatchReady closes, "
                              "it picks up where it left off.", MUTED)
        self.title_meta = self.vt("No monitor yet", MUTED)
        self.verdict = self.vt("Not monitored", GREY, font(20, family=SEMI))
        self.sub = self.vt("Start a monitor and leave it running. A full day shows time-of-day patterns; even an "
                           "hour catches frequent drops.", MUTED)
        self.tiles = {n: VCell(self, n) for n in self.NAMES}
        self.findings = self.vt("Findings show up here once there's data.", MUTED)
        self.request_text = self.vt("After a monitor, this says whether the results justify a maintenance request "
                                    "and who should get it.", MUTED)
        self.saved = self.vt("", MUTED)
        self.seg = self.btn = self.pick = self.chart = self.save = self.copy = None

    def draw(self, W):
        c, t, b = self.c, self._t, self._b
        L, R, g, P = px(2), W - px(18), px(12), px(18)
        w = R - L
        y = 0
        top = y + px(40)
        self.put(self.seg, L + P, top)
        self.put(self.btn, R - P, top, anchor="ne", width=px(184), height=px(34))
        by = top + max(self.seg.winfo_reqheight(), px(34)) + px(16)
        draw_bar(c, L + P, by, R - P, max(1, px(2)), self.progress.frac, self.progress.col)
        sb = b(t(L + P, by + px(12), self.status.text, 11, self.status.fg, width=w - 2 * P))
        h = sb + P - y
        self._card(L, y, w, h, "Day-long monitor")
        y += h + g

        mx = R - P
        if self.title_meta.text:
            mx = self.c.bbox(t(R - P, y + px(14), self.title_meta.text, 11, self.title_meta.fg, anchor="ne"))[0]
        self.put(self.pick, mx - px(14), y + px(8), anchor="ne", width=px(250), height=px(28))
        t(mx - px(14) - px(250) - px(8), y + px(14), "Viewing", 11, LABEL, anchor="ne")
        vy = b(t(L + P, y + px(46), self.verdict.text, color=self.verdict.fg, fnt=self.verdict.font))
        sb = b(t(L + P, vy + px(4), self.sub.text, 12, self.sub.fg, width=w - 2 * P))
        h = sb + P - y
        self._card(L, y, w, h, "Result")
        y += h + g

        cols = 6 if W / K >= 1060 else 3
        cw = (w - g * (cols - 1)) / cols
        names = list(self.tiles)
        for row in range(0, len(names), cols):
            bottoms = [self._figure(self.tiles[names[row + j]], L + j * (cw + g), y, cw) for j in range(cols)]
            rh = max(bottoms) + P - y
            for j in range(cols):
                self._card(L + j * (cw + g), y, cw, rh, names[row + j])
            y += rh + g

        ch = self.chart.winfo_reqheight()
        self.put(self.chart, L + P, y + P, width=w - 2 * P, height=ch)
        self._card(L, y, w, ch + 2 * P)
        y += ch + 2 * P + g

        half = (w - g) / 2
        fb = b(t(L + P, y + px(40), self.findings.text, 12, self.findings.fg, width=half - 2 * P))
        rx = L + half + g
        rb = b(t(rx + P, y + px(40), self.request_text.text, 12, self.request_text.fg, width=half - 2 * P))
        self.put(self.save, rx + P, rb + px(14), width=px(124), height=px(32))
        self.put(self.copy, rx + P + px(132), rb + px(14), width=px(124), height=px(32))
        sb = rb + px(14) + px(32)
        if self.saved.text:
            sb = b(t(rx + P, sb + px(8), self.saved.text, 11, self.saved.fg, width=half - 2 * P))
        h = max(fb, sb) + P - y
        self._card(L, y, half, h, "What we found")
        self._card(rx, y, half, h, "Maintenance request")
        return y + h + px(22)

    def _figure(self, tile, x, y, w):
        P = px(18)
        value, detail, score = tile.data
        vb = self._b(self._t(x + P, y + px(40), value or "—", 20, TEXT, family=NUM))
        col = color_for(score)
        draw_bar(self.c, x + P, vb + px(10), x + P + px(56), px(3),
                 0 if score is None else max(0.04, min(1.0, score / 100)), col)
        if score is not None:
            self._t(x + P + px(64), vb + px(11), word_for(score), 11, col, "bold", anchor="w")
        db = vb + px(20)
        if detail:
            db = self._b(self._t(x + P, db, detail, 11, MUTED, width=w - 2 * P))
        return db


class AboutPage(CanvasPage):
    """Reference text in two columns of cards."""

    def __init__(self, master, sections):
        super().__init__(master)
        self.sections = sections

    def draw(self, W):
        L, R, g, P = px(2), W - px(18), px(12), px(18)
        colw = (R - L - g) / 2
        ys = [0, 0]
        for heading, paras in self.sections:
            col = 0 if ys[0] <= ys[1] else 1
            x, y = L + col * (colw + g), ys[col]
            yy = y + px(40)
            for i, para in enumerate(paras):
                yy = self._b(self._t(x + P, yy + (0 if i == 0 else px(8)), para, 12, TEXT if i == 0 else MUTED,
                                     width=colw - 2 * P))
            h = yy + P - y
            self._card(x, y, colw, h, heading)
            ys[col] = y + h + g
        return max(ys) + px(10)


# ---------- composite parts ----------
class NavRail(tk.Canvas):
    """Icon + small label per view, drawn on one canvas; the active view gets an accent bar on its left edge.
    Mouse: click. Keyboard: Tab to focus, Up/Down to move, Enter to open (or Ctrl+1-4 from anywhere)."""
    ITEMS = (("Check", "\uEC4A"), ("History", "\uE81C"), ("Monitor", "\uE9D9"), ("About", "\uE946"))

    def __init__(self, master, on_select):
        super().__init__(master, bg=SIDE, width=px(72), bd=0, highlightthickness=0, takefocus=1)
        self.on_select = on_select
        self.current = self.hover = self.cursor_item = None
        self.bind("<Motion>", lambda e: self._set_hover(self._at(e.y)))
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", lambda e: self._at(e.y) and self.on_select(self._at(e.y)))
        self.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Down>", lambda e: self._move(1))
        self.bind("<Return>", lambda e: self.cursor_item and self.on_select(self.cursor_item))
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))

    def _at(self, y):
        i = int((y - px(76)) // px(56))
        return self.ITEMS[i][0] if 0 <= i < len(self.ITEMS) and y >= px(76) else None

    def _set_hover(self, name):
        if name != self.hover:
            self.hover = name
            self.configure(cursor="hand2" if name else "")
            self.redraw()

    def _focus(self, on):
        self.cursor_item = (self.cursor_item or self.current) if on else None
        self.redraw()

    def _move(self, step):
        names = [n for n, _ in self.ITEMS]
        i = names.index(self.cursor_item or self.current or names[0])
        self.cursor_item = names[(i + step) % len(names)]
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = px(72)
        cx = w / 2
        self.create_rectangle(cx - px(15), px(20), cx + px(15), px(50), fill=BTN, outline="")
        self.create_text(cx, px(35), text="M", fill="#ffffff", font=font(14, "bold"))
        for i, (name, glyph) in enumerate(self.ITEMS):
            y0 = px(76) + i * px(56)
            active, hover = name == self.current, name == self.hover
            if hover and not active:
                self.create_rectangle(0, y0, w, y0 + px(56), fill="#18212a", outline="")
            if active:
                self.create_rectangle(0, y0 + px(11), max(2, px(2)), y0 + px(45), fill=ACCENT, outline="")
            if name == self.cursor_item:
                self.create_rectangle(px(4), y0 + px(2), w - px(4), y0 + px(54), outline=TEXT, width=1)
            self.create_text(cx, y0 + px(21), text=glyph, font=(ICON, -px(17)),
                             fill=ACCENT if active else TEXT if hover else LABEL)
            self.create_text(cx, y0 + px(43), text=name, font=font(10),
                             fill=TEXT if active or hover else LABEL)

    def select(self, name):
        self.current = name
        self.redraw()


class ViewStack:
    """The middle column: one frame per view. get()/set() mirror the tab view this replaced."""
    HEAD = {"Check": "Is the connection good enough to queue right now?",
            "History": "How this network has scored over the last 14 days",
            "Monitor": "Log the connection for hours, then hand over the report",
            "About": "How MatchReady measures and scores"}

    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.frames, self.current = {}, None

    def add(self, name):
        f = box(self.parent, BG)
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_remove()
        self.frames[name] = f
        return f

    def get(self):
        return self.current

    def set(self, name):
        if self.current == name:
            return
        if self.current:
            self.frames[self.current].grid_remove()
        self.current = name
        self.frames[name].grid()
        self.app.nav.select(name)
        self.app.view_title.configure(text=name)
        self.app.view_sub.configure(text=self.HEAD[name])


class ScoreCard:
    """The score card (dial, verdict, four core numbers, ping trace) in the main grid, plus the top of the
    summary rail (score and forecast with their changes, server, network, forecast note)."""

    def __init__(self, page, rail, on_go=None):
        self.page, self.on_go = page, on_go
        self.prev = None
        self.card = type("CardParts", (), {"meta": page.meta})()   # card.meta, as the app expects
        self.gauge = Gauge(page.c, {"panel": CARD, "raised": TRACK, "text": TEXT, "muted": MUTED, "label": LABEL,
                                    "red": RED, "amber": AMBER, "green": GREEN, "font": FONT, "thickness": 0.07,
                                    "num_font": NUM, "num_weight": "normal"},
                           zones=(scoring.RISKY, scoring.GO), size=236, scale=K)
        page.gauge = self.gauge
        self.gauge.bind("<Button-1>", lambda e: self.on_go and self.idle and self.on_go())
        self.idle = False
        self.verdict, self.caps, self.feel = page.verdict, page.caps, page.feel
        self.stats = page.stats
        self.trace_title = page.trace_title
        self._build_rail(rail)
        self.set_idle(True)
        self.live_key = None
        page.request()

    def _build_rail(self, rail):
        for name in ("age", "score_val", "score_delta", "fc_val", "fc_delta", "server", "server_note", "network",
                     "forecast"):
            setattr(self, name, getattr(rail, name))

    # ---- states
    def set_idle(self, idle):
        """Before the first check (and after a failed or cancelled one) the dial doubles as a start button."""
        self.idle = idle
        if idle:
            self.gauge.clear(text="START", caption="click to run a check", color=ACCENT)
        self.gauge.configure(cursor="hand2" if idle else "")

    def _set_feel(self, text):
        if text:
            self.feel.configure(text=text)
            self.feel.grid()
        else:
            self.feel.grid_remove()

    def set_stats(self, m, final=False):
        """Fill the four core numbers from whatever is measured so far; deltas only on a final result."""
        g = m.get("servers") or {}
        p = (self.prev or {}) if final and self.prev else None
        pg = (p or {}).get("servers") or {}
        bb = m.get("bufferbloat_ms")
        none = ("", LABEL)
        self.stats["ping"].set(fmt(g.get("latency")), delta(g.get("latency"), pg.get("latency")) if p else none)
        self.stats["jitter"].set(fmt(g.get("jitter"), "", 1),
                                 delta(g.get("jitter"), pg.get("jitter"), digits=1) if p else none)
        self.stats["loss"].set(fmt(g.get("loss_pct"), "", 1),
                               delta(g.get("loss_pct"), pg.get("loss_pct"), digits=1) if p else none)
        pbb = (p or {}).get("bufferbloat_ms")
        self.stats["bloat"].set("—" if bb is None else f"+{max(0, bb):.0f}",
                                delta(max(0, bb), max(0, pbb)) if p and bb is not None and pbb is not None else none)

    def reset_measuring(self):
        """Start-of-check state: drop the previous result so nothing stale is mistaken for the new one."""
        self.live_key = None
        self.set_idle(False)
        self.gauge.clear()
        self.verdict.configure(text="Measuring…", fg=GREY, font=font(22, family=SEMI))
        self.caps.configure(text="First estimate in a second or two.", fg=MUTED)
        self._set_feel("")
        for st in self.stats.values():
            st.set("—")
        self.score_val.configure(text="—", fg=TEXT)
        self.score_delta.configure(text="")
        self.fc_val.configure(text="—", fg=TEXT)
        self.fc_delta.configure(text="")
        self.server.configure(text="Pinging the game servers…")
        self.server_note.configure(text="")
        self.forecast.configure(text="Shows up after the check.", fg=MUTED)
        self.age.configure(text="● Live", fg=ACCENT)
        self.card.meta.configure(text="● LIVE", fg=ACCENT)

    def show_live(self, est, game):
        """Predicted result from the partial data so far. While the range is still wide the verdict is only a
        lean, in a muted weight, so an early estimate never reads like the final answer."""
        key = (est["score"], est["verdict"], est["spread"], tuple(est["caps"]), est["pings"] // 10,
               round(game["latency"]))
        if key == self.live_key:
            return
        self.live_key = key
        c = VERDICT_COLOR[est["verdict"]]
        settled = est["spread"] <= SETTLED_SPREAD
        self.gauge.set(est["score"], c, est["low"], est["high"], live=True)
        word = scoring.VERDICT_LABEL[est["verdict"]]
        if settled:
            self.verdict.configure(text=word, fg=c, font=font(22, family=SEMI))
        else:
            self.verdict.configure(text=f"Leaning {word}", fg=MUTED, font=font(22, family=NUM))
        self.caps.configure(text=("Limited by: " + "; ".join(est["caps"])) if est["caps"] else est["feel"],
                            fg=AMBER if est["caps"] else MUTED)
        self._set_feel("")
        self.score_val.configure(text=f"~{est['score']}", fg=c)
        self.age.configure(text=f"● Live prediction, ± {round(est['spread'])}", fg=ACCENT)
        self.server.configure(text=f"{game['primary']} · {game['latency']:.0f} ms so far")
        self.forecast.configure(text=f"Predicted from {est['pings']} pings; the range narrows as the check "
                                     "finishes.", fg=MUTED)

    def show(self, res, game, fc, prev_score=None):
        self.live_key = None
        self.set_idle(False)
        c = VERDICT_COLOR[res["verdict"]]
        self.gauge.set(res["score"], c)
        self.card.meta.configure(text="")
        self.verdict.configure(text=scoring.VERDICT_LABEL[res["verdict"]], fg=c, font=font(22, family=SEMI))
        # the cause sits right under the verdict; the plain-language feel follows it
        self.caps.configure(text=("Limited by: " + "; ".join(res["caps"])) if res["caps"] else res["feel"],
                            fg=AMBER if res["caps"] else MUTED)
        self._set_feel(res["feel"] if res["caps"] else "")
        self.score_val.configure(text=str(res["score"]), fg=c)
        t, col = delta(res["score"], prev_score, lower_better=False)
        self.score_delta.configure(text=t, fg=col)
        if fc:
            t, col = delta(fc["score"], res["score"], lower_better=False)
            self.fc_val.configure(text=f"~{fc['score']}", fg=color_for(fc["score"]))
            self.fc_delta.configure(text=t, fg=col)
            self.forecast.configure(text=f"Likely {fc['low']}–{fc['high']} ({fc['confidence']} confidence). "
                                         f"{fc['text']}", fg=MUTED)
        else:
            self.fc_val.configure(text="—", fg=TEXT)
            self.fc_delta.configure(text="")
            self.forecast.configure(text="Needs a few more checks on this network.", fg=MUTED)
        if game.get("primary"):
            via = " (TCP fallback)" if game.get("method") == "tcp" else ""
            self.server.configure(text=f"{game['primary']}{via} · {game['latency']:.0f} ms")
            self.server_note.configure(text="In-game ping is usually within ±10 ms of this.")
        else:
            self.server.configure(text="Unreachable")
            self.server_note.configure(text="Check your internet connection or VPN, then run the check again.")

    def set_age(self, text, stale):
        self.age.configure(text=text, fg=AMBER if stale else MUTED)


class App(tk.Tk):
    PATH_TILES = ["Ping", "Worst-case ping", "Jitter", "Packet loss",
                  "Lag spikes", "Bufferbloat", "Game traffic (UDP)", "NAT type"]
    SETUP_TILES = ["Download", "Upload", "Connection", "Wi-Fi signal",
                   "Router ping", "DNS lookup", "Background traffic", "PC / VPN"]
    TILES = PATH_TILES + SETUP_TILES

    def __init__(self):
        global K
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # crisp text on scaled displays
        except (AttributeError, OSError):
            pass
        super().__init__()
        K = max(1.0, self.winfo_fpixels("1i") / 96)
        pick_fonts(self)
        self.configure(bg=BG)
        self.title("MatchReady")
        self.set_window_icon()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(px(1340), sw - 40)}x{min(px(880), sh - 80)}")
        self.minsize(min(px(1060), sw - 40), min(px(660), sh - 80))
        self.history = History()
        self.events = queue.Queue()
        self.busy = False
        self.last_alert_ok = True
        self.watch_job = None
        self.checked_at = None
        self.current_network = None
        self.toast = None
        self.cancel_evt = threading.Event()
        self.full_running = False    # a full check (cancellable), as opposed to a watch-mode quick check
        self.last_result = None      # (m, result, fc, checked_at): restored when a check is cancelled
        self.probe_lock = threading.Lock()  # one measurement at a time: your checks and the monitor's

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.nav = NavRail(self, self.select_tab)
        self.nav.grid(row=0, column=0, sticky="ns")

        main = self.main = box(self, BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        head = box(main)
        head.grid(row=0, column=0, sticky="we", padx=(px(28), px(24)), pady=(px(22), px(14)))
        titles = box(head)
        titles.pack(side="left")
        self.view_title = Text(titles, "Check", 18, TEXT)
        self.view_title.pack(anchor="w")
        self.view_sub = Text(titles, "", 11, MUTED)
        self.view_sub.pack(anchor="w", pady=(px(2), 0))
        self.netlabel = Text(head, f"{region()} region", 11, MUTED, bg=CARD, padx=px(14), pady=px(7))
        self.netlabel.pack(side="right")
        content = box(main)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.tabs = ViewStack(self, content)

        rail = box(self, RAIL, width=px(300))
        rail.grid(row=0, column=2, sticky="ns")
        rail.grid_propagate(False)
        rail.rowconfigure(0, weight=1)
        rail.columnconfigure(0, weight=1)
        self.rail = RailPage(rail)
        self.rail.grid(row=0, column=0, sticky="nsew")

        self.build_check(self.tabs.add("Check"))
        self.build_rail(self.rail)
        self.build_history(self.tabs.add("History"))
        self.build_monitor(self.tabs.add("Monitor"))
        self.build_about(self.tabs.add("About"))
        self.select_tab("Check")
        for seq in ("<Control-r>", "<Control-R>", "<F5>"):
            self.bind(seq, lambda e: self.start_full())
        for i, name in enumerate(("Check", "History", "Monitor", "About"), 1):
            self.bind(f"<Control-Key-{i}>", lambda e, n=name: self.select_tab(n))
        self.bind("<Escape>", lambda e: self.on_escape())
        self.bind_all("<MouseWheel>", self._wheel)
        self.after(100, self.pump)
        self.after(20000, self.tick_age)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(1500, self.resume_or_finish_monitor)
        self.update_spark()

        self.updater = Updater()
        self.upd_pending = self.upd_dismissed = self.upd_failed = None
        self.upd_building = False
        self.upd_started, self.upd_stage = 0.0, (0, "running tests")
        self.build_update_banner()
        if self.updater.enabled:
            self.after(3000, self.poll_updates)
        # Published copies (not your dev build, which updates from source above) check GitHub Releases.
        self.rel_repo = release_update.load_repo()
        self.rel_dismissed, self.rel_busy = None, False
        if self.updater.frozen and self.rel_repo and not self.updater.enabled:
            self.after(8000, self.check_release)

    def set_window_icon(self):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "matchready"
        icon = base / "icon.ico"
        if icon.is_file():
            try:
                self.iconbitmap(str(icon))
            except tk.TclError:
                pass

    def _wheel(self, e):
        """Scroll whichever scroll area is under the pointer."""
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and not hasattr(w, "wheel"):
            w = getattr(w, "master", None)
        if w is not None:
            w.wheel(e.delta)

    # ---------- summary rail ----------
    def build_rail(self, rail):
        self.card = ScoreCard(self.check_page, rail, on_go=self.start_full)
        rail.run_btn = self.run_btn = Button(rail.c, RUN_LABEL, self.on_run_button, "primary", icon="\uE72C",
                                             height=36)
        rail.watch = self.watch = Toggle(rail.c, "Watch mode", self.toggle_watch)
        Tooltip([self.watch], lambda: "Quick check every 2 minutes; alerts if it drops below RISKY.")
        self.progress, self.status = rail.progress, rail.status
        self.step_line, self.watch_status = rail.step_line, rail.watch_status
        self.spark, self.spark_label = rail.spark, rail.spark_label
        rail.request()

    def update_spark(self):
        rows = self.history.rows(since=time.time() - 14 * 86400, network=self.current_network)
        scores = [r[2] for r in rows if r[2] is not None]
        self.spark.set_data(scores)
        n = len(scores)
        self.spark_label.configure(text=f"Your score over time · last {min(n, 30)} checks" if n >= 2
                                   else "Your score over time")

    # ---------- Check view ----------
    def build_check(self, outer):
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        page = self.check_page = self.check_scroll = CheckPage(outer)
        page.grid(row=0, column=0, sticky="nsew", padx=(px(20), 0))
        self.score_card = page
        self.tiles = {}
        for group, names in enumerate((self.PATH_TILES, self.SETUP_TILES)):
            for name in names:
                self.tiles[name] = page.cell(name, group)
        self.hints, self.groups = page.hints, page.groups

    def set_advice(self, lines, empty=None, tone=AMBER):
        """lines: advice strings, one strip each; empty: a single muted strip when there's nothing to list."""
        self.check_page.set_advice(lines, empty, tone)

    def draw_activity(self, rows):
        """One line under the progress bar: which step is running and what it has measured so far."""
        total = len(rows)
        done = sum(r["state"] in ("done", "skip") for r in rows)
        run = next((r for r in rows if r["state"] == "run"), None)
        labels = dict(runner.STEPS)
        if run:
            text = f"Step {min(done + 1, total)} of {total} · {labels[run['id']]}" + \
                   (f" · {run['detail']}" if run["detail"] else "")
        else:
            text = f"All {total} steps done" if done == total and total else ""
        if self.step_line.cget("text") != text:
            self.step_line.configure(text=text)

    def draw_spark(self, title, traces, counts=None):
        self.check_page.set_trace(title, traces, counts)

    def trace_empty(self):
        self.check_page.set_trace("", None)

    def set_side(self, m):
        """Packets-delivered donut and speed bars from whatever is measured so far."""
        g = m.get("servers") or {}
        udp = m.get("udp") or {}
        self.check_page.set_side(g.get("loss_pct"), udp.get("loss_pct"), m.get("download_mbps"),
                                 m.get("upload_mbps"), "download_mbps" in m, "upload_mbps" in m)

    def apply_live(self, snap):
        """Paint a partial snapshot: activity, graph, predicted scores and whichever measurements exist yet."""
        self.draw_activity(snap["activity"])
        self.draw_spark(snap["trace_title"], snap["traces"], snap.get("trace_counts"))
        m = snap["m"]
        est = snap["estimate"]
        if est and m.get("servers"):
            prev = self.live_ema
            smooth = est["score"] if prev is None else 0.5 * prev + 0.5 * est["score"]  # damp early wobble
            self.live_ema = smooth
            score = round(smooth)
            self.card.show_live({**est, "score": score, "verdict": scoring.verdict_for(score)}, m["servers"])
        self.fill_tiles(snap["view"])
        self.card.set_stats(snap["view"])
        now = time.monotonic()
        if now - self.live_flag_t >= 1.0:
            self.live_flag_t = now
            self.set_side(snap["view"])
            self.refresh_flagged()

    def reset_tiles(self):
        for t in self.tiles.values():
            t.set("—", "", None)

    def refresh_flagged(self):
        for hint, cells in zip(self.hints, self.groups):
            subs = [c.data[2] for c in cells if c.data[2] is not None]
            bad = [s for s in subs if s < 80]
            if not subs:
                hint.configure(text="")
            elif bad:
                hint.configure(text=f"{len(bad)} need attention", fg=RED if min(bad) < 60 else AMBER)
            else:
                hint.configure(text="All good", fg=GREEN)

    def select_tab(self, name):
        self.tabs.set(name)
        self.on_tab()

    def tick_age(self):
        self.update_age()
        self.after(20000, self.tick_age)

    def update_age(self):
        if self.checked_at is None:
            return
        secs = time.time() - self.checked_at
        stale = secs >= STALE_AFTER_S
        self.card.set_age(("Old result. Run again before you queue" if stale else
                           f"Checked {age_text(secs)} · {self.current_network}"), stale)
        if not self.busy:
            self.status.configure(text="Close downloads and streams, then run it again." if stale else
                                  "Ctrl+R runs it again from any view.", fg=AMBER if stale else MUTED)

    # ---------- History view ----------
    def build_history(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        card = Card(tab, "Scores")
        card.grid(row=0, column=0, sticky="nsew", padx=(px(22), px(24)), pady=(0, px(22)))
        self.hist_label = card.meta
        card.body.rowconfigure(0, weight=1)
        self.chart = HistoryChart(card.body, {
            "bg": CARD, "panel": CARD, "raised": TRACK, "line": LINE, "text": TEXT, "muted": LABEL,
            "red": RED, "amber": AMBER, "green": GREEN, "series": AMBER, "font": FONT,
            "risky": scoring.RISKY, "go": scoring.GO})
        self.chart.grid(row=0, column=0, sticky="nsew")

    def draw_history(self):
        net = getattr(self, "current_network", None)
        rows = self.history.rows(since=time.time() - 14 * 86400, network=net)
        self.hist_label.configure(text=f"{len(rows)} checks in the last 14 days" + (f" on {net}" if net else ""))
        prof = predict.hourly_profile([(r[0], r[2]) for r in rows if r[2] is not None])
        self.chart.set_data(rows, prof)

    def on_tab(self):
        if self.tabs.get() == "History":
            self.draw_history()
        elif self.tabs.get() == "Monitor":
            self.update_monitor_view(force=True)

    # ---------- Monitor view ----------
    RATING_COLOR = {"stable": GREEN, "pattern": AMBER, "flaky": AMBER, "unstable": RED}

    def build_monitor(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        page = self.monitor_page = MonitorPage(tab)
        page.grid(row=0, column=0, sticky="nsew", padx=(px(20), 0))
        c = page.c
        page.seg = self.mon_len = Segmented(c, list(monitor.DURATIONS), monitor.DEFAULT_DURATION)
        page.btn = self.mon_btn = Button(c, "Start monitoring", self.toggle_monitor, "primary", icon="\uE768",
                                         width=184)
        page.pick = self.mon_pick = Dropdown(c, self.pick_monitor)
        page.chart = self.mon_chart = MonitorChart(c, {
            "bg": CARD, "panel": CARD, "raised": TRACK, "line": LINE, "text": TEXT, "muted": LABEL, "red": RED,
            "amber": AMBER, "green": GREEN, "series": SERVER_LINE, "router": LABEL, "font": FONT})
        self.mon_chart.configure(height=px(400))
        page.save = self.mon_save = Button(c, "Save report", self.save_monitor_report, "primary", width=124,
                                           height=32)
        page.copy = self.mon_copy = Button(c, "Copy summary", self.copy_monitor_report, "secondary", width=124,
                                           height=32)
        for btn in (self.mon_save, self.mon_copy):
            btn.configure(state="disabled")
        self.mon_progress, self.mon_status = page.progress, page.status
        self.mon_title, self.mon_verdict, self.mon_sub = page.title_meta, page.verdict, page.sub
        self.mon_tiles = page.tiles
        self.mon_findings, self.mon_request, self.mon_saved = page.findings, page.request_text, page.saved

        self.mon = None              # the running monitor: {"session", "end", "start", "stop", "next"}
        self.mon_view = None         # session shown on the tab
        self.mon_samples, self.mon_analysis = [], {}
        self.mon_draw_t = 0.0
        self.refresh_monitor_list()

    def resume_or_finish_monitor(self):
        """At startup: carry on with a monitor that was interrupted, or close one whose time ran out."""
        row = self.history.unfinished_monitor()
        if not row:
            return
        sid, started, planned_end = row
        if planned_end - time.time() > 2 * monitor.SAMPLE_EVERY_S:
            self.start_monitor(resume=(sid, started, planned_end))
            self.alert("Resumed your connection monitor. It runs until " + monitor.when(planned_end) + ".", good=True)
        else:
            samples = self.history.monitor_samples(sid)
            self.history.end_monitor(sid, now=samples[-1]["ts"] if samples else planned_end)
            self.refresh_monitor_list()
            self.view_monitor(sid)

    def toggle_monitor(self):
        if self.mon:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self, resume=None):
        if resume:
            sid, started, end = resume
        else:
            secs = monitor.DURATIONS[self.mon_len.get()]
            started = time.time()
            sid, end = self.history.start_monitor(secs, now=started), started + secs
        if self.watch.get():  # the monitor supersedes watch mode
            self.watch.deselect()
            self.toggle_watch()
        self.watch.configure(state="disabled")
        self.mon = {"session": sid, "start": started, "end": end, "stop": threading.Event(), "next": time.time()}
        self.mon_btn.configure(text="Stop and diagnose", kind="secondary", icon="")
        self.mon_len.configure(state="disabled")
        self.refresh_monitor_list()
        self.view_monitor(sid)
        threading.Thread(target=self._monitor_loop, args=(self.mon,), daemon=True).start()
        self.after(1000, self.monitor_tick)

    def stop_monitor(self):
        if self.mon:
            self.mon["stop"].set()
            self.mon_btn.configure(state="disabled", text="Stopping…")

    def _monitor_loop(self, mon):
        """Background thread: one sample a minute until the end time or a stop request."""
        es_continuous, es_system_required = 0x80000000, 0x00000001
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)  # no sleep
        except (AttributeError, OSError):
            pass
        last = self.history.monitor_samples(mon["session"])
        next_full = 0.0 if not any(s.get("kind") == "full" for s in last) else \
            max(s["ts"] for s in last if s.get("kind") == "full") + monitor.FULL_EVERY_S
        try:
            while not mon["stop"].is_set() and time.time() < mon["end"]:
                began = time.time()
                with self.probe_lock:  # never overlap a check you started yourself
                    kind = "full" if began >= next_full else "light"
                    m = result = fc = None
                    try:
                        prior = self.history.last_full_metrics()
                        m = runner.run_full(prior=prior) if kind == "full" else runner.run_light(prior)
                        result, fc = self._score_and_save(kind, m)
                        sample = monitor.sample_from(m, result, kind)
                        if kind == "full":
                            next_full = began + monitor.FULL_EVERY_S
                    except Exception as e:
                        traceback.print_exc()
                        sample = monitor.failed_sample(began, f"{type(e).__name__}: {e}"[:200])
                        m = None
                self.history.add_monitor_sample(mon["session"], sample)
                mon["next"] = began + monitor.SAMPLE_EVERY_S
                self.events.put(("mon_sample", mon["session"], sample, m, result, fc))
                mon["stop"].wait(max(1.0, mon["next"] - time.time()))
        finally:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)
            except (AttributeError, OSError):
                pass
            self.history.end_monitor(mon["session"])
            self.events.put(("mon_done", mon["session"], mon["stop"].is_set()))

    def monitor_tick(self):
        mon = self.mon
        if not mon:
            return
        now = time.time()
        total, done = mon["end"] - mon["start"], now - mon["start"]
        self.mon_progress.set(max(0.0, min(1.0, done / total)))
        n = len(self.mon_samples) if self.mon_view == mon["session"] else 0
        nxt = int(max(0, mon["next"] - now))
        left = monitor.dur(max(0, mon["end"] - now))
        self.mon_status.configure(
            text=f"Running · {left} left · {n} check{'s' if n != 1 else ''} logged · "
                 + (f"next check in {nxt} s" if nxt else "checking now…")
                 + ". Keeps this PC awake; leave MatchReady open.", fg=MUTED)
        self.watch_status.configure(text=f"Watch mode is paused while the monitor runs ({left} left).", fg=ACCENT)
        self.after(1000, self.monitor_tick)

    def on_monitor_sample(self, session, sample, m, result, fc):
        if session == self.mon_view:
            self.mon_samples.append(sample)
            self.update_monitor_view()
        if m is not None and not self.busy:
            self.render(m, result, fc)  # keep the Check view current too
        score = monitor.score(sample)
        ok = score >= ALERT_BELOW
        if not ok and self.last_alert_ok:
            self.alert(f"Connection dropped: score {score} ({word_for(score)}).\nThe monitor is logging it.")
        elif ok and not self.last_alert_ok:
            self.alert(f"Connection recovered: score {score}.", good=True)
        self.last_alert_ok = ok

    def on_monitor_done(self, session, stopped):
        self.mon = None
        self.watch.configure(state="normal")
        self.watch_status.configure(text="", fg=MUTED)
        self.mon_btn.configure(state="normal", text="Start monitoring", kind="primary", icon="")
        self.mon_len.configure(state="normal")
        self.mon_progress.set(1)
        self.refresh_monitor_list()
        self.view_monitor(session)
        a = self.mon_analysis
        if a.get("n"):
            saved = self.save_monitor_report(open_folder=False)
            self.alert(f"Monitor {'stopped' if stopped else 'finished'}: {a['rating_text']}"
                       + (f"\nReport saved to {saved.parent}." if saved else ""), good=a["rating"] == "stable")
        self.mon_status.configure(text=f"Monitor {'stopped' if stopped else 'finished'}. Results below; the report "
                                       "was saved automatically.", fg=MUTED)

    # ---- viewing a session
    @staticmethod
    def _session_label(row):
        sid, started, planned_end, ended, n = row
        state = "running" if ended is None else monitor.dur((ended or planned_end) - started)
        return f"{datetime.fromtimestamp(started).strftime('%b %d, ')}{monitor.clock(started)} · {state} · {n} checks"

    def refresh_monitor_list(self):
        self.mon_rows = self.history.monitor_sessions()
        labels = [self._session_label(r) for r in self.mon_rows] or ["No monitors yet"]
        self.mon_pick.configure(values=labels if self.mon_rows else [])
        current = next((self._session_label(r) for r in self.mon_rows if r[0] == self.mon_view), labels[0])
        self.mon_pick.set(current)
        if self.mon_view is None and self.mon_rows:
            self.view_monitor(self.mon_rows[0][0])

    def pick_monitor(self, label):
        row = next((r for r in self.mon_rows if self._session_label(r) == label), None)
        if row:
            self.view_monitor(row[0])

    def view_monitor(self, session):
        self.mon_view = session
        self.mon_samples = self.history.monitor_samples(session)
        row = next((r for r in self.mon_rows if r[0] == session), None)
        if row:
            self.mon_pick.set(self._session_label(row))
        self.mon_saved.configure(text="")
        self.update_monitor_view(force=True)

    def update_monitor_view(self, force=False):
        a = self.mon_analysis = monitor.analyze(self.mon_samples)
        running = bool(self.mon and self.mon["session"] == self.mon_view)
        for btn in (self.mon_save, self.mon_copy):
            btn.configure(state="normal" if a.get("n") else "disabled")
        T = self.mon_tiles
        if not a.get("n"):
            self.mon_title.configure(text="Waiting for the first check…" if running else "No monitor yet", fg=MUTED)
            self.mon_verdict.configure(text="Measuring" if running else "Not monitored", fg=GREY)
            for t in T.values():
                t.set("—", "", None)
            self.mon_chart.set_data([], {})
            return
        span = monitor.dur(a["end"] - a["start"])
        self.mon_title.configure(text=("● Live · " if running else "") + f"{span} · {a['n']} checks",
                                 fg=ACCENT if running else MUTED)
        early = a["end"] - a["start"] < 3600
        if early:
            self.mon_verdict.configure(text="Collecting data" if running else "Too short to judge", fg=MUTED)
            self.mon_sub.configure(text="Keep it running. Patterns need at least a few hours; the verdict firms up "
                                        "as data comes in." if running else
                                   "Under an hour of data. The numbers below are real, but run a longer monitor "
                                   "before drawing conclusions.", fg=MUTED)
        else:
            self.mon_verdict.configure(text={"stable": "Stable", "pattern": "Unstable at certain times",
                                             "flaky": "Occasionally unstable", "unstable": "Unstable"}[a["rating"]],
                                       fg=self.RATING_COLOR[a["rating"]])
            self.mon_sub.configure(text=a["rating_text"].split(". ", 1)[-1], fg=TEXT)
        incs = a["incidents"]
        T["Usable for games"].set(f"{a['go_pct']:.0f}%", "of checks were GO", 100 if a["go_pct"] >= 95 else
                                  70 if a["go_pct"] >= 80 else 30)
        longest = max((i["duration"] for i in incs), default=0)
        T["Problem periods"].set(str(len(incs)), f"longest {monitor.dur(longest)}" if incs else "none",
                                 100 if not incs else 70 if len(incs) <= 2 else 30)
        T["Dropouts"].set(str(a["dropouts"]), "nothing answered", 100 if not a["dropouts"] else
                          70 if a["dropouts"] <= 1 else 30)
        T["Packet loss"].set(f"{a['loss_avg']:.1f}%", f"{a['loss_samples_pct']:.0f}% of checks lost some",
                             100 if a["loss_avg"] < 0.3 else 70 if a["loss_avg"] < 1 else 30)
        if a["ms_p50"] is not None:
            T["Ping"].set(f"{a['ms_p50']:.0f} ms", f"95% under {a['ms_p95']:.0f} ms",
                          100 if a["ms_p95"] - a["ms_p50"] < 25 else 70 if a["ms_p95"] - a["ms_p50"] < 60 else 30)
        if a["bad_spans"]:
            T["Worst hours"].set(", ".join(monitor.span_label(x, y) for x, y in a["bad_spans"][:2]),
                                 "problems repeat then", 30)
        else:
            T["Worst hours"].set("None", f"{a['hours_covered']} of 24 hours covered" if a["hours_covered"] < 24
                                 else "no time-of-day pattern", None if a["hours_covered"] < 6 else 100)
        self.mon_findings.configure(text="\n".join("• " + t for _, t in a["findings"]), fg=TEXT)
        mt = a["maintenance"]
        if early:
            self.mon_request.configure(text="Too early to say. A monitor needs a few hours at least.", fg=MUTED)
        elif mt["justified"]:
            self.mon_request.configure(
                text=f"Yes. These results justify asking {mt['who']} to take a look. Save the report and attach "
                     "both files: the summary explains the problem in plain language and the CSV has every "
                     "measurement with a timestamp.", fg=TEXT)
        else:
            self.mon_request.configure(text="Not really. The connection was mostly healthy, so a maintenance "
                                            "request wouldn't have much evidence behind it.", fg=TEXT)
        now = time.monotonic()
        if force or self.tabs.get() == "Monitor" and now - self.mon_draw_t >= 5:
            self.mon_draw_t = now
            self.mon_chart.set_data(self.mon_samples, a)

    def monitor_report_text(self):
        return monitor.report_text(self.mon_analysis)

    def save_monitor_report(self, open_folder=True):
        if not self.mon_analysis.get("n"):
            return None
        folder = Path.home() / "Documents" / "MatchReady reports"
        stem = "MatchReady monitor " + datetime.fromtimestamp(self.mon_analysis["start"]).strftime("%Y-%m-%d %H%M")
        try:
            txt_path, csv_path = monitor.export(self.mon_samples, self.mon_analysis, folder, stem)
        except OSError as e:
            self.mon_saved.configure(text=f"Couldn't save the report: {e.strerror or e}", fg=RED)
            return None
        self.mon_saved.configure(text=f"Saved {txt_path.name} and {csv_path.name} in {folder}", fg=MUTED)
        if open_folder:
            try:
                os.startfile(folder)
            except OSError:
                pass
        return txt_path

    def copy_monitor_report(self):
        self.clipboard_clear()
        self.clipboard_append(self.monitor_report_text())
        self.mon_saved.configure(text="Summary copied. Paste it into an email or maintenance ticket.", fg=MUTED)

    def on_close(self):
        if self.mon:
            left = monitor.dur(max(0, self.mon["end"] - time.time()))
            if not messagebox.askyesno("MatchReady", f"The connection monitor has {left} left. Closing pauses "
                                       "it; it picks up where it left off the next time you open MatchReady.\n\n"
                                       "Close anyway?", parent=self):
                return
        self.destroy()

    # ---------- About view ----------
    ABOUT = [
        ("How it works", [
            "Pings several game datacenters in your region (by default Chicago, Dallas and Iowa) for ~14 s to "
            "measure ping, jitter, lag spikes and packet loss. The nearest one counts, since that's where "
            "matchmaking puts you; loss is pooled across all of them. Falls back to TCP timing if ping is blocked.",
            "Pings your router to tell 'my Wi-Fi is bad' apart from 'the campus/ISP network is bad'.",
            "Sends UDP (STUN) packets, the protocol games actually use, to check that game traffic gets through "
            "and to detect NAT type.",
            "Runs a short Cloudflare speed test while pinging, which measures bufferbloat: how much lag spikes "
            "when the connection is busy.",
            "Reads Wi-Fi signal, band, channel crowding and link rate from Windows, plus CPU load, VPNs and "
            "background traffic."]),
        ("Scoring", [
            "One score for online gaming in general, since shooters, fighting, sports and racing games all "
            "suffer from the same things. Weights: packet loss 28%, jitter and lag spikes 24%, ping 24%, "
            "bufferbloat 12%, UDP health 7%, Wi-Fi quality 5%. One weak core factor (ping, jitter, loss, UDP) "
            "drags the whole score down.",
            "80+ = GO, 60–79 = RISKY, below 60 = DON'T QUEUE. Loss over 2%, dropouts or blocked UDP cap the "
            "score no matter how good the rest looks."]),
        ("Live view", [
            "Within a second or two of pressing Run, you see a predicted score with a ± range, and a graph draws "
            "every ping as it arrives. The verdict shows as a lean (\"Leaning RISKY\") until the range is within "
            "±5, so an early guess never reads as the answer. Until UDP and bufferbloat are measured, the "
            "prediction uses your last check on the same connection, which is why it starts wide."]),
        ("Prediction", [
            "Every check is saved. The 30-minute forecast blends the current result with the recent trend and "
            "how this network usually performs at this hour. Watch mode adds a quick check every 2 minutes, "
            "which trains the forecast faster and alerts you if things drop below RISKY."]),
        ("Day-long monitor", [
            "Runs a quick check every minute for 1, 8 or 24 hours (plus a full check with a speed test every few "
            "hours) and logs every result. It finds problem periods, dropouts, the hours when the connection gets "
            "worse, and whether the fault is between your PC and the router (Wi-Fi, cable) or past it (building, "
            "campus or internet provider). The saved report (a summary plus a CSV of every measurement, in "
            "Documents\\MatchReady reports) is written for a maintenance request.",
            "The monitor keeps the PC awake while it runs. If MatchReady closes, it picks up where it left off "
            "the next time it opens."]),
        ("Limits", [
            "Most games don't publish their match-server IPs, so this measures datacenters in the same areas. "
            "In-game ping is usually within ±10 ms. Change the region by editing targets.json. Sudden campus "
            "congestion can't be predicted, but the History view shows when it usually happens."]),
        ("Shortcuts and files", [
            "Ctrl+R or F5 runs a check from any view; Esc cancels it. Ctrl+1–4 switch views. Esc also dismisses "
            "an alert.",
            "Server list: targets.json (editable)", f"History database: {db_path()}",
            f"Version {__version__}"]),
    ]

    def build_about(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        AboutPage(tab, self.ABOUT).grid(row=0, column=0, sticky="nsew", padx=(px(20), 0))

    # ---------- running ----------
    def start_full(self):
        if self.busy:
            self.status.configure(text="A check is already running. It will finish in a few seconds.", fg=MUTED)
            return
        self.busy = self.full_running = True
        self.cancel_evt = threading.Event()
        self.run_btn.configure(text="Cancel check", kind="secondary", icon="")
        self.progress.set(0, ACCENT)
        self.status.configure(text="About 30 seconds. Esc cancels.", fg=MUTED)
        # fresh slate: nothing from the last result should be mistaken for this check
        self.live_active, self.live_ema, self.live_flag_t = True, None, 0.0
        self.card.reset_measuring()
        self.reset_tiles()
        self.refresh_flagged()
        self.set_side({})
        self.set_advice([], "Advice appears when the check finishes.")
        self.draw_activity([{"id": sid, "state": "wait", "detail": ""} for sid, _ in runner.STEPS])
        self.trace_empty()
        self.title("MatchReady · measuring…")
        threading.Thread(target=self._worker, args=("full",), daemon=True).start()

    def _worker(self, kind):
        cancel = self.cancel_evt if kind == "full" else None
        if not self.probe_lock.acquire(blocking=False):
            self.events.put(("progress", 0.0, "Waiting for the current check to finish…"))
            self.probe_lock.acquire()
        try:
            if cancel is not None and cancel.is_set():  # cancelled while waiting for the lock
                self.events.put(("cancelled",))
                return
            self._measure(kind, cancel)
        finally:
            self.probe_lock.release()

    def _measure(self, kind, cancel=None):
        try:
            prog = (lambda f, t: self.events.put(("progress", f, t))) if kind == "full" else (lambda f, t: None)
            if kind == "full":
                m = runner.run_full(prog, lambda snap: self.events.put(("live", snap)),
                                    self.history.last_full_metrics(), cancel=cancel)
                if cancel is not None and cancel.is_set():
                    raise runner.Cancelled()  # finished in the same instant as the click: the click wins
            else:
                m = runner.run_light(self.history.last_full_metrics(), prog)
            result, fc = self._score_and_save(kind, m)
            self.events.put((kind, m, result, fc))
        except runner.Cancelled:
            self.events.put(("cancelled",))
        except Exception:
            if cancel is not None and cancel.is_set():
                self.events.put(("cancelled",))  # a probe tripping over the abort is still a cancel
            else:
                self.events.put(("error", kind, traceback.format_exc()))

    def _score_and_save(self, kind, m):
        """Score a check, forecast from this network's history, then record it."""
        result = runner.score_all(m)
        past = self.history.rows(since=time.time() - 60 * 86400, network=m["network"])
        fc = predict.forecast(result["score"], [(r[0], r[2]) for r in past])
        self.history.add(kind, result["score"], m["network"], m if kind == "full" else None)
        return result, fc

    def pump(self):
        latest_live = None
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "live":
                    latest_live = ev[1]  # only the newest snapshot matters
                elif ev[0] == "progress":
                    self.progress.set(ev[1])
                elif ev[0] == "full":
                    self.show_full(*ev[1:])
                elif ev[0] == "light":
                    self.show_light(*ev[1:])
                elif ev[0] == "cancelled":
                    self.show_cancelled()
                elif ev[0] == "error":
                    self.busy = self.full_running = False
                    self.end_live()
                    card = self.card
                    card.reset_measuring()
                    card.verdict.configure(text="No result", fg=GREY)
                    card.caps.configure(text="The check didn't finish.")
                    card.server.configure(text="—")
                    card.age.configure(text="", fg=MUTED)
                    card.card.meta.configure(text="")
                    card.set_idle(True)  # the dial becomes a start button again
                    self.title("MatchReady")
                    self.run_btn.configure(state="normal", text="Try again", kind="primary", icon="")
                    self.progress.set(1, RED)
                    self.step_line.configure(text="")
                    self.status.configure(text="The check couldn't finish. Make sure you're online and not behind "
                                               "a Wi-Fi login page, then try again.", fg=RED)
                    print(ev[2])
                elif ev[0] == "mon_sample":
                    self.on_monitor_sample(*ev[1:])
                elif ev[0] == "mon_done":
                    self.on_monitor_done(*ev[1:])
                elif ev[0] == "upd_line":
                    self.note_build_line(ev[1])
                elif ev[0] == "upd_done":
                    self.update_built(*ev[1:])
                elif ev[0] == "rel_found":
                    self.on_release_found(ev[1])
                elif ev[0] == "rel_progress":
                    self.show_banner(f"Downloading MatchReady {ev[1]}… {ev[2] * 100:.0f}%")
                elif ev[0] == "rel_ready":
                    self.install_release(ev[1])
                elif ev[0] == "rel_failed":
                    self.on_release_failed(*ev[1:])
        except queue.Empty:
            pass
        if latest_live is not None and self.live_active:
            try:
                self.apply_live(latest_live)
            except Exception:
                traceback.print_exc()  # a drawing glitch must never stop the check from finishing
        self.after(100, self.pump)

    def end_live(self):
        self.live_active = False  # the last trace stays up so you can see what the check measured

    def on_run_button(self):
        if self.full_running:
            self.cancel_check()
        else:
            self.start_full()

    def on_escape(self):
        if self.toast is not None:
            self.dismiss_toast()
        elif self.full_running:
            self.cancel_check()

    def cancel_check(self):
        """Ask the running check to stop; probes notice within a sample (~0.1 s, a few s at worst)."""
        if not self.full_running or self.cancel_evt.is_set():
            return
        self.cancel_evt.set()
        self.run_btn.configure(state="disabled", text="Stopping…")
        self.status.configure(text="Stopping the check…", fg=MUTED)

    def show_cancelled(self):
        """Nothing from a cancelled check is saved or shown; the previous result (if any) comes back."""
        self.busy = self.full_running = False
        self.end_live()
        self.run_btn.configure(state="normal", text=RUN_LABEL, kind="primary", icon="")
        self.progress.set(0, ACCENT)
        self.step_line.configure(text="")
        self.trace_empty()
        self.reset_tiles()
        if self.last_result:
            m, result, fc, at = self.last_result
            prev = self.card.prev
            self.render(m, result, fc, restoring=True)
            self.card.prev = prev
            self.checked_at = at          # keep the real age, so an old result still says it's old
            self.last_result = (m, result, fc, at)
            self.update_age()
            self.status.configure(text="Check cancelled; nothing was saved. Showing your previous result.", fg=MUTED)
        else:
            card = self.card
            card.reset_measuring()
            card.verdict.configure(text="Not tested")
            card.caps.configure(text="Check cancelled. Run one before you queue.")
            card.server.configure(text="—")
            card.forecast.configure(text="Shows up after a check.")
            card.age.configure(text="Not tested yet", fg=MUTED)
            card.card.meta.configure(text="")
            card.set_idle(True)
            self.set_side({})
            self.refresh_flagged()
            self.set_advice([], "Advice shows up here after your first check.")
            self.title("MatchReady")
            self.status.configure(text="Check cancelled; nothing was saved.", fg=MUTED)

    def show_full(self, m, result, fc):
        self.busy = self.full_running = False
        self.end_live()
        self.step_line.configure(text=f"All {len(runner.STEPS)} steps done")
        self.run_btn.configure(state="normal", text="Run again", kind="primary", icon="")
        # the bar says "finished", not "good": it takes the verdict's colour
        self.progress.set(1, VERDICT_COLOR[result["verdict"]])
        self.render(m, result, fc)
        if self.tabs.get() == "History":
            self.draw_history()

    def render(self, m, result, fc, restoring=False):
        """Paint a result (full or watch-mode quick check) into the card, rail, advice and measurements."""
        prev = self.last_result
        if not restoring:
            self.card.prev = prev[0] if prev and prev[0] is not m else None
            self.prev_score = prev[1]["score"] if prev and prev[0] is not m else None
        self.current_network = m["network"]
        self.checked_at = time.time()
        self.last_result = (m, result, fc, self.checked_at)
        self.netlabel.configure(text=f"{m['network']} · {region()}")
        self.card.network.configure(text=f"{m['network']} · {region()}")
        self.card.show(result, m["servers"], fc, getattr(self, "prev_score", None))
        self.card.set_stats(m, final=True)
        self.set_side(m)
        self.title(f"MatchReady · {scoring.VERDICT_LABEL[result['verdict']]} ({result['score']})")

        lines = list(result["advice"])
        if not lines:  # a cap can hold the score down with no tip attached; never say "good" over a bad verdict
            lines = list(result["caps"])
        if lines:
            self.set_advice(lines, tone=VERDICT_COLOR[result["verdict"]] if result["verdict"] != "go" else AMBER)
        else:
            self.set_advice(["Nothing significant. Good to queue."], tone=GREEN)
        self.fill_tiles(m)
        self.refresh_flagged()
        self.update_age()
        self.update_spark()

    def fill_tiles(self, m):
        T = self.tiles
        # While a check is running m is partial: the servers are absent until there are enough pings, and other
        # measurements arrive over time. Only touch the tiles whose data exists.
        g = m.get("servers")
        if g:
            lat, p95 = g.get("latency"), g.get("p95")
            T["Ping"].set(fmt(lat, " ms"), g.get("primary") or "unreachable",
                          scoring.curve(lat, PROFILE["latency"]) if lat else 0)
            T["Worst-case ping"].set(fmt(p95, " ms"), "95% of pings were faster",
                                     scoring.curve(p95, PROFILE["latency"]) if p95 is not None else None)
            jit = g.get("jitter")
            T["Jitter"].set(fmt(jit, " ms", 1), "variation between pings",
                            scoring.curve(jit, PROFILE["jitter"]) if jit is not None else None)
            T["Packet loss"].set(fmt(g["loss_pct"], "%", 1), f"longest dropout: {g.get('max_loss_burst', 0)} in a row",
                                 scoring.curve(g["loss_pct"], PROFILE["loss"]))
            sp = g.get("spike_pct")
            T["Lag spikes"].set(fmt(sp, "%", 1), "pings 40+ ms over normal",
                                scoring.curve(sp, scoring.SPIKES) if sp is not None else None)
        bb = m.get("bufferbloat_ms")
        ll = m.get("loaded_loss_pct") or 0
        if bb is not None:
            # show whichever number drives the status, so the value never contradicts its word
            if ll >= 1:
                T["Bufferbloat"].set(f"{ll:.0f}% loss", f"under load, +{max(0, bb):.0f} ms extra lag",
                                     scoring.bufferbloat_subscore(m))
            else:
                T["Bufferbloat"].set("+" + fmt(max(0, bb), " ms"), "extra lag when busy",
                                     scoring.bufferbloat_subscore(m))
        udp = m.get("udp") or {}
        if udp:
            usub = scoring.udp_subscore(m)
            if not udp.get("ok"):
                value = "Blocked"
            elif usub is not None and usub < 80:
                value = fmt(udp.get("loss_pct"), "% loss", 1)
            else:
                value = "Gets through"
            T["Game traffic (UDP)"].set(value, f"{fmt(udp.get('loss_pct'), '% loss', 1)}, {fmt(udp.get('median'), ' ms')}",
                                        usub)
        if "nat" in udp:
            nat = udp["nat"]
            T["NAT type"].set(nat, "can break party joins and voice chat" if nat == "Strict" else "matchmaking-friendly"
                              if nat != "Unknown" else "", {"Strict": 60, "Unknown": None}.get(nat, 100))
        down, up = m.get("download_mbps"), m.get("upload_mbps")
        # the key is present-but-None only when the speed test ran and moved no data (blocked or rate-limited)
        if "download_mbps" in m:
            T["Download"].set(fmt(down, " Mbps") if down is not None else "Unavailable",
                              "games need < 1 Mbps" if down is not None else "speed test blocked or rate-limited",
                              None if down is None else 100 if down >= 5 else 70 if down >= 1.5 else 0)
        if "upload_mbps" in m:
            T["Upload"].set(fmt(up, " Mbps") if up is not None else "Unavailable",
                            "games need < 0.5 Mbps" if up is not None else "speed test blocked or rate-limited",
                            None if up is None else 100 if up >= 2 else 70 if up >= 0.5 else 0)
        w = m.get("wifi") or {}
        ctype = (m.get("connection") or {}).get("type")
        if ctype == "wifi" and not w:
            pass  # the Wi-Fi scan is still running
        elif ctype is None:
            pass  # connection type not known yet
        elif ctype == "wifi" and w.get("connected"):
            T["Connection"].set(f"Wi-Fi {w.get('band') or ''}".strip(),
                                f"ch {w.get('channel')}, {fmt(w.get('rx_mbps'), ' Mbps link')}, "
                                f"{fmt(w.get('aps_same_channel'))} APs sharing", scoring.wifi_subscore(m))
            T["Wi-Fi signal"].set(f"{fmt(w.get('rssi_dbm'), ' dBm')}", f"{fmt(w.get('signal_pct'), '%')} · {w.get('ssid') or ''}",
                                  scoring.curve(w.get("rssi_dbm"), scoring.RSSI) if w.get("rssi_dbm") is not None else None)
        else:
            T["Connection"].set("Ethernet" if m["connection"]["type"] == "wired" else "Unknown",
                                m["connection"].get("interface") or "", 100 if m["connection"]["type"] == "wired" else None)
            T["Wi-Fi signal"].set("n/a", "wired connection", None)
        gw = m.get("gateway") or {}
        gsub = None
        if gw.get("received"):
            gsub = 100 if gw["loss_pct"] == 0 and gw["jitter"] < 5 else 70 if gw["loss_pct"] < 1 else 30
        if gw:
            T["Router ping"].set(fmt(gw.get("avg"), " ms", 1), f"{fmt(gw.get('jitter'), ' ms jitter', 1)}, "
                                 f"{fmt(gw.get('loss_pct'), '% loss', 1)}", gsub)
        sysm = m.get("system") or {}
        dns = sysm.get("dns_ms")
        if dns is not None:
            T["DNS lookup"].set(fmt(dns, " ms"), "affects matchmaking/login only", 100 if dns < 80 else 70 if dns < 250 else 40)
        if "heavy_apps" in sysm:
            bg = (sysm.get("bg_down_mbps") or 0) + (sysm.get("bg_up_mbps") or 0)
            apps = sysm.get("heavy_apps") or []
            T["Background traffic"].set(f"{bg:.1f} Mbps", ", ".join(apps)[:40] or "other traffic on this PC",
                                        100 if bg < 1 else 70 if bg < 3 else 40)
        if "cpu" in sysm:
            vpn = sysm.get("vpn") or []
            T["PC / VPN"].set(f"CPU {fmt(sysm.get('cpu'), '%')}", f"VPN: {', '.join(vpn)}" if vpn else "no VPN",
                              40 if vpn else 100 if (sysm.get("cpu") or 0) < 85 else 60)

    # ---------- watch mode ----------
    def toggle_watch(self):
        if self.watch.get():
            self.watch_status.configure(text="Watching. First quick check in a few seconds.", fg=MUTED)
            self.schedule_watch(5)
        else:
            if self.watch_job:
                self.after_cancel(self.watch_job)
                self.watch_job = None
            self.watch_status.configure(text="")

    def schedule_watch(self, seconds):
        self.watch_job = self.after(int(seconds * 1000), self.watch_tick)

    def watch_tick(self):
        if not self.watch.get() or self.mon:
            return
        if not self.busy:
            self.busy = True
            threading.Thread(target=self._worker, args=("light",), daemon=True).start()
        self.schedule_watch(WATCH_INTERVAL_S)

    def show_light(self, m, result, fc):
        self.busy = False
        score = result["score"]
        self.render(m, result, fc)
        now = datetime.now().strftime("%I:%M %p").lstrip("0")
        self.watch_status.configure(text=f"Last quick check {now}: {score}, {scoring.VERDICT_LABEL[result['verdict']]}",
                                    fg=color_for(score))
        ok = score >= ALERT_BELOW
        if not ok and self.last_alert_ok:
            self.alert(f"Connection dropped: score {score} ({scoring.VERDICT_LABEL[result['verdict']]}).\n"
                       "Hold off on ranked for now.")
        elif ok and not self.last_alert_ok:
            self.alert(f"Connection recovered: score {score}.", good=True)
        self.last_alert_ok = ok

    # ---------- self-update ----------
    def build_update_banner(self):
        self.banner = box(self.main, INFO_BG)
        self.banner_text = autowrap(Text(self.banner, "", 12, TEXT, wraplength=px(600)), pad=320)
        self.banner_text.pack(side="left", fill="x", expand=True, padx=px(14), pady=px(10))
        self.banner_second = Button(self.banner, "", None, "secondary", width=90, height=30)
        self.banner_main = Button(self.banner, "", None, "primary", width=130, height=30)
        self.banner.grid(row=1, column=0, sticky="we", padx=(px(22), px(24)), pady=(0, px(12)))
        self.banner.grid_remove()

    def show_banner(self, text, main=None, second=None, color=INFO_BG):
        """main/second = (label, callback) or None."""
        self.banner.configure(bg=color)
        self.banner_text.configure(text=text, bg=color)
        for btn in (self.banner_main, self.banner_second):
            btn.pack_forget()
        for btn, spec in ((self.banner_main, main), (self.banner_second, second)):
            if spec:
                btn.configure(text=spec[0], command=spec[1], state="normal", highlightbackground=color)
                btn.pack(side="right", padx=(0, px(10)), pady=px(8))
        if not self.banner.winfo_ismapped():
            self.banner.grid()

    def hide_banner(self):
        self.banner.grid_remove()

    @staticmethod
    def _names(files):
        names = [f.rsplit("/", 1)[-1] for f in files]
        return ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")

    def poll_updates(self):
        try:
            if self.upd_building:
                self.show_build_progress()  # keeps the elapsed time ticking through quiet stretches
            else:
                self._check_updates()
        except Exception:
            traceback.print_exc()
        # Short interval = short wait after you stop editing: a build starts the poll after the code
        # stops changing, so ~1.5-3 s instead of the old 4-8 s.
        self.after(1500, self.poll_updates)

    # (substring of a build-log line, label) in the order the build prints them
    BUILD_STAGES = (("passed", "tests passed, analyzing code"), ("Analyzing", "analyzing code"),
                    ("Building PKG", "packing the app"), ("Appending PKG", "finishing"))

    def note_build_line(self, line):
        for i, (needle, label) in enumerate(self.BUILD_STAGES, 1):
            if needle in line and i >= self.upd_stage[0]:
                self.upd_stage = (i, label)
        self.show_build_progress()

    def show_build_progress(self):
        step, label = self.upd_stage
        self.show_banner(f"Building update, step {step} of {len(self.BUILD_STAGES) + 1}: {label} "
                         f"({time.time() - self.upd_started:.0f} s, usually about 20 s)")

    def _check_updates(self):
        state, current, changed = self.updater.check()
        key = tuple(sorted(current.items()))
        if state == "current":
            self.upd_pending = self.upd_failed = None
            self.hide_banner()
        elif key == self.upd_dismissed:
            self.hide_banner()
        elif state == "staged":
            self.show_banner(f"Update ready ({self._names(changed)} changed).",
                             ("Restart now", self.apply_update), ("Later", lambda: self.dismiss_update(key)))
        elif not self.updater.frozen:
            self.show_banner(f"Code changed ({self._names(changed)}). Restart to load it.",
                             ("Restart now", self.apply_update), ("Later", lambda: self.dismiss_update(key)))
        elif key == self.upd_failed:
            pass  # keep the failure banner until the code changes again
        elif key == self.upd_pending:
            self.start_update_build(key)  # unchanged for a full poll: you've stopped editing
        else:
            self.upd_pending = key
            self.show_banner(f"Changes detected ({self._names(changed)}). Building an update when you stop editing…")

    def dismiss_update(self, key):
        self.upd_dismissed = key
        self.hide_banner()

    def start_update_build(self, key):
        self.upd_building = True
        self.upd_started, self.upd_stage = time.time(), (0, "running tests")
        self.show_build_progress()

        def work():
            ok = self.updater.rebuild(lambda line: self.events.put(("upd_line", line)))
            self.events.put(("upd_done", ok, key))
        threading.Thread(target=work, daemon=True).start()

    def update_built(self, ok, key):
        self.upd_building = False
        if ok:
            self._check_updates()  # -> "Update ready" (or a fresh build if you edited during this one)
            return
        log = self._read_log()
        if "another build is already running" in log:
            # e.g. build.bat: its staged result is picked up by the next poll
            self.upd_pending = None
            self.show_banner("Another build (build.bat?) is running. Waiting for it to finish…")
            return
        self.upd_failed = key
        reason = "tests failed" if "tests failed" in log else "build error"
        self.show_banner(f"Update failed ({reason}). You're still on the working version. Fix and save to retry.",
                         ("View log", lambda: os.startfile(self.updater.log_path)),
                         ("Retry", lambda: self.start_update_build(key)), color=ERR_BG)

    def _read_log(self):
        try:
            return self.updater.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def apply_update(self):
        if self.busy:
            self.show_banner("Restarting as soon as the current check finishes…")
            self.after(1000, self.apply_update)
            return
        self.updater.restart()
        self.destroy()

    # ---------- updates from GitHub Releases (published copies) ----------
    RELEASE_RECHECK_MS = 6 * 3600 * 1000

    def check_release(self):
        self.after(self.RELEASE_RECHECK_MS, self.check_release)
        if self.rel_busy:
            return

        def work():
            try:
                self.events.put(("rel_found", release_update.check(self.rel_repo, __version__)))
            except Exception:
                traceback.print_exc()  # offline or GitHub unreachable: try again at the next check
        threading.Thread(target=work, daemon=True).start()

    def on_release_found(self, upd):
        if not upd or upd["version"] == self.rel_dismissed or self.rel_busy:
            return
        text = f"MatchReady {upd['version']} is available (you have {__version__})."
        later = ("Later", lambda: self.dismiss_release(upd["version"]))
        if release_update.can_replace(sys.executable):
            self.show_banner(text, ("Update now", lambda: self.download_release(upd)), later)
        else:  # e.g. installed under Program Files: we can't overwrite it, so send them to the download
            self.show_banner(text + " Download it to replace this copy.",
                             ("Download", lambda: os.startfile(upd["page"])), later)

    def dismiss_release(self, version):
        self.rel_dismissed = version
        self.hide_banner()

    def download_release(self, upd):
        self.rel_busy = True
        self.show_banner(f"Downloading MatchReady {upd['version']}…")

        def work():
            try:
                path = release_update.download(
                    upd, lambda f: self.events.put(("rel_progress", upd["version"], f)))
                self.events.put(("rel_ready", path))
            except Exception as e:
                traceback.print_exc()
                self.events.put(("rel_failed", upd, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def on_release_failed(self, upd, reason):
        self.rel_busy = False
        self.show_banner(f"The update couldn't be downloaded ({reason}). You're still on {__version__}.",
                         ("Retry", lambda: self.download_release(upd)),
                         ("Open page", lambda: os.startfile(upd["page"])), color=ERR_BG)

    def install_release(self, path):
        if self.busy:
            self.show_banner("Update downloaded. Restarting as soon as the current check finishes…")
            self.after(1000, lambda: self.install_release(path))
            return
        swap_and_restart(path, Path(sys.executable), delete_source=True)
        self.destroy()

    def alert(self, msg, good=False):
        self.bell()
        try:
            ctypes.windll.user32.FlashWindow(ctypes.windll.user32.GetParent(self.winfo_id()), True)
        except (AttributeError, OSError):
            pass
        self.dismiss_toast()
        toast = self.toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg=CARD)
        tk.Frame(toast, bg=GREEN if good else RED, height=max(2, px(3))).pack(fill="x")
        caps(toast, "MatchReady", color=GREEN if good else RED).pack(anchor="w", padx=px(16), pady=(px(12), 0))
        Text(toast, msg, 12, TEXT, wraplength=px(320)).pack(anchor="w", padx=px(16), pady=(px(4), 0))
        Text(toast, "Click or press Esc to dismiss", 11, MUTED).pack(anchor="w", padx=px(16), pady=(px(6), px(14)))
        toast.update_idletasks()
        w, h = px(360), toast.winfo_reqheight()  # sized to the message, so long ones never clip
        toast.geometry(f"{w}x{h}+{self.winfo_screenwidth() - w - px(24)}+{self.winfo_screenheight() - h - px(70)}")
        toast.bind("<Button-1>", lambda e: self.dismiss_toast())
        toast.bind("<Escape>", lambda e: self.dismiss_toast())
        toast.after(20000, lambda: self.dismiss_toast(toast))

    def dismiss_toast(self, only=None):
        if self.toast is not None and (only is None or only is self.toast):
            try:
                self.toast.destroy()
            except Exception:
                pass
            self.toast = None


def main():
    App().mainloop()
