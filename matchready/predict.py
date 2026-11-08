"""Short-term forecast: blends the current score with the recent trend and this time slot's history."""
import statistics
import time
from datetime import datetime

DAY = 86400


def _slot_matches(ts, now_dt):
    """Same hour (+/-1) on the same kind of day (weekday vs weekend)."""
    d = datetime.fromtimestamp(ts)
    same_daytype = (d.weekday() >= 5) == (now_dt.weekday() >= 5)
    hour_gap = min(abs(d.hour - now_dt.hour), 24 - abs(d.hour - now_dt.hour))
    return same_daytype and hour_gap <= 1


def _trend_per_hour(points):
    """Least-squares slope (score per hour) of [(ts, score)]."""
    if len(points) < 3:
        return None
    xs = [(t - points[0][0]) / 3600 for t, _ in points]
    ys = [s for _, s in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den < 1e-6:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def forecast(current, history, now=None, horizon_min=30):
    """
    current: this run's score. history: [(ts, score)] from earlier runs (score may be None).
    Returns dict with predicted score, range, confidence and a sentence.
    """
    now = now or time.time()
    now_dt = datetime.fromtimestamp(now)
    hist = [(t, s) for t, s in history if s is not None and t < now]

    slot = [s for t, s in hist if now - t > 3 * 3600 and _slot_matches(t, now_dt)]
    recent = [(t, s) for t, s in hist if now - t <= 2 * 3600] + [(now, current)]

    pred, parts = float(current), []
    slot_med = slot_low = None
    if len(slot) >= 3:
        slot_med = statistics.median(slot)
        slot_low = sorted(slot)[max(0, int(len(slot) * 0.1) - 1)] if len(slot) >= 10 else min(slot)
        pred = 0.65 * current + 0.35 * slot_med
        parts.append(f"usually {slot_med:.0f} around {now_dt.strftime('%I %p').lstrip('0')} on "
                     f"{'weekends' if now_dt.weekday() >= 5 else 'weekdays'}")

    slope = _trend_per_hour(recent)
    if slope is not None:
        drift = max(-15.0, min(15.0, slope * horizon_min / 60))
        pred += 0.5 * drift
        if abs(slope) >= 8:
            parts.append(f"{'improving' if slope > 0 else 'getting worse'} over the last couple of hours")
        else:
            parts.append("steady over the last couple of hours")

    pred = max(0, min(100, round(pred)))
    spread = 6 if len(slot) >= 10 else 10 if len(slot) >= 3 else 15
    if slot_low is not None and slot_low < current - 15:
        parts.append(f"this time slot has dipped as low as {slot_low:.0f}")
    n = len(slot) + len(recent) - 1
    confidence = "high" if n >= 15 else "medium" if n >= 5 else "low"
    if not parts:
        parts.append("not enough history yet (run more checks at different times to train this)")
    text = "; ".join(parts)
    return {
        "score": pred, "low": max(0, pred - spread), "high": min(100, pred + spread),
        "confidence": confidence, "text": text[0].upper() + text[1:] + ".",
    }


def hourly_profile(history):
    """{hour: median score} for the history chart."""
    by_hour = {}
    for t, s in history:
        if s is not None:
            by_hour.setdefault(datetime.fromtimestamp(t).hour, []).append(s)
    return {h: statistics.median(v) for h, v in sorted(by_hour.items())}
