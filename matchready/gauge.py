"""Speedometer-style dial for the score: a 270° gradient arc, zone band, needle and a big number.

Smoothness comes from doing the expensive drawing once and only compositing per frame:
- At construction, everything with curved edges is drawn at 3x and scaled down (clean anti-aliasing):
  the static dial face, the ring shape, the needle and the hub.
- An "angle map" (each pixel's position around the dial, in hundredths of a degree) turns any value
  into a reveal mask with one linear Image.point() call, so the arc grows continuously, with a soft
  1-pixel leading edge, instead of in drawn segments. The colour gradient comes from the same map.
- The needle is a pre-drawn sprite rotated with bicubic filtering.
- The motion is eased by the clock (ease-out cubic), not by frame count, so a late frame never
  makes the needle jump or crawl. A new value mid-sweep retargets from wherever the needle is.
A frame is a few milliseconds of C-level image work, then one PhotoImage.paste().
Numbers stay native Tk text so they're crisp.
"""
import math
import time
import tkinter as tk

from PIL import Image, ImageChops, ImageDraw, ImageTk

START, SWEEP = 135.0, 270.0   # Pillow angles: degrees clockwise from 3 o'clock; 135 = bottom-left
SS = 3                        # supersampling for the one-time drawings
FRAME_MS = 8                  # tick often; the eased position comes from the clock
ANGLE_SCALE = 100             # angle map units per degree


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    """t=1 -> a, t=0 -> b (both RGB tuples)."""
    return tuple(round(x * t + y * (1 - t)) for x, y in zip(a, b))


def angle_for(value):
    return START + SWEEP * max(0.0, min(100.0, value)) / 100.0


def ease_out_cubic(p):
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) ** 3


class Gauge(tk.Canvas):
    """theme: panel, raised, text, muted, red, amber, green (hex) + font; zones = (risky, go) thresholds."""

    def __init__(self, master, theme, zones=(60, 80), size=300, scale=None):
        k = scale or (master._get_widget_scaling() if hasattr(master, "_get_widget_scaling") else 1.0)
        self.k = k
        self.W = int(size * k)
        self.r = self.W * 0.40
        self.thick = self.r * float(theme.get("thickness", 0.14))
        self.cx, self.cy = self.W / 2, self.r + self.thick * 1.6
        self.H = int(self.cy + self.r * 0.95)
        super().__init__(master, width=self.W, height=self.H, bg=theme["panel"], highlightthickness=0)
        self.t, self.zones = theme, zones
        self.shown = self.target = None   # None = no score (empty dial, idle text)
        self.color = theme["muted"]
        self.range = None
        self.prefix, self.sub = "", "/ 100"
        self.idle_text, self.idle_color = "--", theme["muted"]
        self._job = None
        self._anim = None                  # (from_value, to_value, t0, duration)
        self._grad_cache = {}

        self._prepare()
        self._photo = ImageTk.PhotoImage(self._base, master=self)
        self.image_item = self.create_image(0, 0, anchor="nw", image=self._photo)
        f = theme["font"]
        self.num_font = theme.get("num_font", f)
        self.num_weight = theme.get("num_weight", "bold")
        label_r = self.r - max(self.thick * 2.15, self.r * 0.2)
        for v in range(0, 101, 20):   # tick labels just inside the arc
            a = math.radians(angle_for(v))
            self.create_text(self.cx + label_r * math.cos(a), self.cy + label_r * math.sin(a), text=str(v),
                             fill=theme.get("label", theme["muted"]), font=(f, -max(9, int(self.r * 0.075))))
        self.number = self.create_text(self.cx, self.cy + self.r * 0.50, text="--", fill=theme["muted"],
                                       font=(self.num_font, -int(self.r * 0.40), self.num_weight))
        self.caption = self.create_text(self.cx, self.cy + self.r * 0.80, text="/ 100",
                                        fill=theme.get("label", theme["muted"]),
                                        font=(f, -max(10, int(self.r * 0.085))))
        self._draw()

    # ---- public
    def set(self, value, color, low=None, high=None, live=False):
        """Point the dial at value (0-100). The needle eases there; live adds '~' and a ± range band."""
        self.color = color
        self.range = (low, high) if low is not None and high is not None else None
        self.prefix = "~" if live else ""
        self.sub = f"± {round((high - low) / 2)}" if live and self.range else "/ 100"
        if self.shown is None:
            self.shown = 0.0
        value = float(max(0.0, min(100.0, value)))
        if value != self.target or self._anim is None:
            # longer sweeps take a little longer, like a physical needle
            dur = 0.45 + 0.75 * abs(value - self.shown) / 100.0
            self._anim = (self.shown, value, time.perf_counter(), dur)
        self.target = value
        self._schedule()

    def clear(self, text="--", caption="/ 100", color=None):
        """Back to an empty dial with no needle; text/color replace the number (e.g. a "GO" prompt)."""
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self._anim = None
        self.shown = self.target = None
        self.range, self.prefix, self.sub = None, "", caption
        self.idle_text, self.idle_color = text, color or self.t["muted"]
        self.color = self.t["muted"]
        self._draw()

    # ---- one-time drawings
    def _box(self, radius, scale=SS):
        cx, cy, rr = self.cx * scale, self.cy * scale, radius * scale
        return [cx - rr, cy - rr, cx + rr, cy + rr]

    def _down(self, img):
        return img.resize((self.W, self.H), Image.BOX)

    def _prepare(self):
        t = self.t
        big = (self.W * SS, self.H * SS)
        w = int(self.thick * SS)

        # static face: track, zone band, ticks
        face = Image.new("RGB", big, _rgb(t["panel"]))
        d = ImageDraw.Draw(face)
        d.arc(self._box(self.r), START, START + SWEEP, fill=_rgb(t["raised"]), width=w)
        risky, go = self.zones
        band_r = self.r + self.thick * 0.95
        bw = max(2, int(self.thick * 0.22 * SS))
        for lo, hi, col in ((0, risky, t["red"]), (risky, go, t["amber"]), (go, 100, t["green"])):
            d.arc(self._box(band_r), angle_for(lo) + 0.6, angle_for(hi) - 0.6,
                  fill=_mix(_rgb(col), _rgb(t["panel"]), 0.75), width=bw)
        tick = max(self.thick, self.r * 0.07)
        for v in range(0, 101, 10):
            a = math.radians(angle_for(v))
            r0 = (self.r - self.thick * 0.5 - tick * 0.5) * SS
            r1 = (self.r - self.thick * 0.5 - tick * (0.85 if v % 20 else 1.1)) * SS
            d.line([(self.cx * SS + r0 * math.cos(a), self.cy * SS + r0 * math.sin(a)),
                    (self.cx * SS + r1 * math.cos(a), self.cy * SS + r1 * math.sin(a))],
                   fill=_rgb(t["muted"]), width=max(1, int(SS * self.k)))
        self._base = self._down(face)

        # ring shape (where the value arc can be), as an anti-aliased alpha mask
        ring = Image.new("L", big, 0)
        ImageDraw.Draw(ring).arc(self._box(self.r), START, START + SWEEP, fill=255, width=w)
        self._ring = self._down(ring)

        # angle map: hundredths of a degree past START for every pixel; the bottom gap snaps to the nearest end
        full = int(SWEEP * ANGLE_SCALE)
        data = []
        cx, cy = self.cx, self.cy
        for y in range(self.H):
            dy = y + 0.5 - cy
            for x in range(self.W):
                a = math.degrees(math.atan2(dy, x + 0.5 - cx)) % 360.0   # clockwise from 3 o'clock
                rel = (a - START) % 360.0
                if rel > SWEEP:
                    rel = 0.0 if rel > (SWEEP + 360.0) / 2 else SWEEP
                data.append(int(rel * ANGLE_SCALE))
        self._angle = Image.new("I", (self.W, self.H))
        self._angle.putdata(data)
        self._full = full
        # leading-edge softness: about one pixel at the ring's radius, in angle-map units
        self._soft = max(1.0, math.degrees(1.0 / self.r) * ANGLE_SCALE)

        # needle sprite: white, pointing right (0°) from the sprite centre; alpha carries the shape
        tip = self.r - self.thick * 0.6
        half = int(math.ceil(tip + 4))
        size = 2 * half
        nb = Image.new("L", (size * SS, size * SS), 0)
        nd = ImageDraw.Draw(nb)
        c = half * SS
        base = self.r * 0.035 * SS
        nd.polygon([(c + tip * SS, c), (c, c + base), (c - base * 0.9, c), (c, c - base)], fill=255)
        hub = self.r * 0.06 * SS
        nd.ellipse([c - hub, c - hub, c + hub, c + hub], fill=255)
        alpha = nb.resize((size, size), Image.BOX)
        self._needle = Image.merge("RGBA", (*Image.new("RGB", (size, size), _rgb(t["text"])).split(), alpha))
        self._needle_half = half
        # hub dot (verdict colour) mask
        dot = int(math.ceil(self.r * 0.035)) * 2 + 2
        db = Image.new("L", (dot * SS, dot * SS), 0)
        hr = self.r * 0.06 * 0.45 * SS
        dc = dot * SS / 2
        ImageDraw.Draw(db).ellipse([dc - hr, dc - hr, dc + hr, dc + hr], fill=255)
        self._dot = db.resize((dot, dot), Image.BOX)

    # ---- per-frame compositing
    def _reveal(self, value):
        """Alpha mask: 255 for angles below value, a ~1 px soft edge, 0 beyond."""
        a = value / 100.0 * self._full
        scale = 255.0 / self._soft
        return self._angle.point(lambda x: x * -scale + (a + self._soft / 2) * scale).convert("L")

    def _gradient(self, color):
        """Value-arc colour: dim at the start, full colour at the far end (cached per colour)."""
        g = self._grad_cache.get(color)
        if g is None:
            col, panel = _rgb(color), _rgb(self.t["panel"])
            chans = []
            for cc, pc in zip(col, panel):
                lo = pc + (cc - pc) * 0.45            # colour at angle 0
                span = (cc - pc) * 0.55 / self._full  # change per angle unit
                chans.append(self._angle.point(lambda x, lo=lo, span=span: x * span + lo).convert("L"))
            g = self._grad_cache[color] = Image.merge("RGB", chans)
        return g

    def _draw(self):
        t = self.t
        frame = self._base.copy()
        if self.shown is not None:
            col = _rgb(self.color)
            if self.range:
                lo, hi = self.range
                band = ImageChops.subtract(self._reveal(hi), self._reveal(lo))
                band_col = _mix(col, _rgb(t["raised"]), 0.30)
                frame.paste(band_col, (0, 0), ImageChops.multiply(band, self._ring))
            frame.paste(self._gradient(self.color), (0, 0), ImageChops.multiply(self._reveal(self.shown), self._ring))
            needle = self._needle.rotate(-angle_for(self.shown), resample=Image.BICUBIC)
            h = self._needle_half
            frame.paste(needle, (round(self.cx - h), round(self.cy - h)), needle)
            dh = self._dot.width / 2
            frame.paste(col, (round(self.cx - dh), round(self.cy - dh)), self._dot)
        self._photo.paste(frame)
        if self.shown is None:
            text, fill = self.idle_text, self.idle_color
        else:
            text, fill = f"{self.prefix}{round(self.shown)}", self.color
        # words (an idle "START") get a smaller size than numbers so they clear the tick labels
        size = -int(self.r * (0.40 if len(text) <= 3 else 0.22))
        if getattr(self, "_num_size", None) != size:
            self._num_size = size
            self.itemconfigure(self.number, font=(self.num_font, size, self.num_weight))
        if self.itemcget(self.number, "text") != text or self.itemcget(self.number, "fill") != fill:
            self.itemconfigure(self.number, text=text, fill=fill)
        if self.itemcget(self.caption, "text") != self.sub:
            self.itemconfigure(self.caption, text=self.sub)

    def _schedule(self):
        if self._job is None:
            self._tick()

    def _tick(self):
        self._job = None
        if self._anim is None or self.target is None:
            return
        start, end, t0, dur = self._anim
        p = (time.perf_counter() - t0) / dur
        if p >= 1.0:
            self.shown, self._anim = end, None
        else:
            self.shown = start + (end - start) * ease_out_cubic(p)
            self._job = self.after(FRAME_MS, self._tick)
        self._draw()
