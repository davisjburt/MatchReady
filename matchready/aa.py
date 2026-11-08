"""Anti-aliased chart shapes for Tk canvases.

Tk canvas lines, ovals and polygons have no anti-aliasing on Windows, so sloped lines come out as stairs.
An AALayer collects shapes in canvas coordinates, draws them with Pillow at SS x size, shrinks the result
with a box filter (cheap, and gives clean soft edges) and places it on the canvas as one image. Text stays
as canvas text on top, since Tk already renders text smoothly.
"""
from PIL import Image, ImageDraw, ImageTk

SS = 3  # supersampling factor


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def smooth(pts, step=3.0):
    """A monotone cubic curve through pts (sorted by x). It never overshoots the data, so peaks and dips stay
    where they were measured; it only rounds the corners between them. Dense series are returned as-is."""
    n = len(pts)
    if n < 3:
        return list(pts)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    if min(h) <= 0 or (xs[-1] - xs[0]) / (n - 1) < 2 * step:
        return list(pts)
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else
                  3 * (h[i - 1] + h[i]) / ((2 * h[i] + h[i - 1]) / d[i - 1] + (h[i] + 2 * h[i - 1]) / d[i])
                  for i in range(1, n - 1)] + [d[-1]]
    out = []
    for i in range(n - 1):
        k = max(1, int(h[i] / step))
        for j in range(k):
            t = j / k
            t2, t3 = t * t, t * t * t
            out.append((xs[i] + t * h[i],
                        (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
                        + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1]))
    out.append(pts[-1])
    return out


class AALayer:
    """Shapes for the region (x0, y0, w, h) of a canvas, painted over `bg`.

    `shapes` records (kind, colour) for every shape added, so tests can check what was drawn."""

    def __init__(self, x0, y0, w, h, bg):
        self.x0, self.y0 = x0, y0
        self.w, self.h = max(1, int(round(w))), max(1, int(round(h)))
        self.bg = bg
        self.ops, self.shapes = [], []

    def _p(self, x, y):
        return ((x - self.x0) * SS, (y - self.y0) * SS)

    def line(self, pts, color, width=1.0, dash=None):
        """A polyline through [(x, y), ...]. dash=(on, off) in px is supported for straight 2-point lines."""
        if len(pts) < 2:
            return
        self.shapes.append(("line", color))
        if dash and len(pts) == 2:
            (ax, ay), (bx, by) = pts
            length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            on, off = dash
            s = 0.0
            while s < length:
                e = min(s + on, length)
                self.ops.append(("line", [(ax + (bx - ax) * s / length, ay + (by - ay) * s / length),
                                          (ax + (bx - ax) * e / length, ay + (by - ay) * e / length)],
                                 color, width))
                s += on + off
            return
        self.ops.append(("line", list(pts), color, width))

    def polygon(self, pts, fill):
        if len(pts) >= 3:
            self.shapes.append(("polygon", fill))
            self.ops.append(("polygon", list(pts), fill))

    def rect(self, x0, y0, x1, y1, fill):
        self.shapes.append(("rect", fill))
        self.ops.append(("rect", (x0, y0, x1, y1), fill))

    def oval(self, cx, cy, r, fill, outline=None, width=1.0):
        self.shapes.append(("oval", outline or fill))
        self.ops.append(("oval", (cx, cy, r), fill, outline, width))

    def render(self):
        img = Image.new("RGB", (self.w * SS, self.h * SS), _rgb(self.bg))
        d = ImageDraw.Draw(img)
        for op in self.ops:
            kind = op[0]
            if kind == "line":
                _, pts, color, width = op
                w = max(1, int(round(width * SS)))
                d.line([self._p(*p) for p in pts], fill=_rgb(color), width=w, joint="curve")
                if w > 2:  # round caps
                    r = w / 2
                    for x, y in (self._p(*pts[0]), self._p(*pts[-1])):
                        d.ellipse((x - r, y - r, x + r, y + r), fill=_rgb(color))
            elif kind == "polygon":
                d.polygon([self._p(*p) for p in op[1]], fill=_rgb(op[2]))
            elif kind == "rect":
                (ax, ay), (bx, by) = self._p(*op[1][:2]), self._p(*op[1][2:])
                d.rectangle((min(ax, bx), min(ay, by), max(ax, bx) - 1, max(ay, by) - 1), fill=_rgb(op[2]))
            elif kind == "oval":
                (cx, cy, r), fill, outline, width = op[1], op[2], op[3], op[4]
                x, y = self._p(cx, cy)
                r *= SS
                d.ellipse((x - r, y - r, x + r, y + r), fill=_rgb(fill) if fill else None,
                          outline=_rgb(outline) if outline else None,
                          width=max(1, int(round(width * SS))) if outline else 0)
        return img.reduce(SS)

    def paste(self, canvas, tags=()):
        """Draw onto `canvas` at (x0, y0). The PhotoImage is kept on the canvas until its tag is deleted."""
        photo = ImageTk.PhotoImage(self.render(), master=canvas)
        item = canvas.create_image(self.x0, self.y0, image=photo, anchor="nw", tags=tags)
        keep = canvas.__dict__.setdefault("_aa_images", {})
        live = set(canvas.find_all())
        for k in [k for k in keep if k not in live]:  # drop images whose canvas item is gone
            del keep[k]
        keep[item] = photo
        return item
