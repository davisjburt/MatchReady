# MatchReady design system

Direction pinned by the user (2026-11-06): the dark analytics dashboard from their reference image. A slim
icon nav rail, flat near-square cards one shade above the page, table rows as separate strips on the page
background, tiny uppercase labels over regular-weight numbers, thin colored bars, a thin donut and a bar chart,
and a right-hand summary rail with a blue action button. The speedometer dial stays. No avatar or profile.

## Contract
- **THESIS:** MatchReady reads like a performance report on your own connection. The score card leads (dial,
  verdict, four core numbers, ping trace), the rail always answers "ready to queue?" with the Run button, and
  every measurement sits in a strip with a health bar. It refuses chunky speed-test chrome and walls of tiles.
- **OWN-WORLD:** cool blue-charcoal surfaces (nav < page < card/rail), square corners, no borders, 2–4 px
  bars on a slate track, 10 px uppercase labels, regular-weight numbers, with teal / yellow / coral for health
  and two blues for data series.
- **FIRST VIEWPORT (Check):** nav rail; page header "Check" with the network chip; score card (dial left;
  verdict, cause and Ping/Jitter/Packet loss/Bufferbloat right; ping-during-the-check chart below) beside a
  narrow column with the Packets delivered donut and Speed bars; then "What's affecting you" strips; then the
  two-group measurement table. Rail: Ready to queue?, Score and Next 30 min with ▲/▼ changes, Game server,
  Network, Forecast, **Run check** (primary), Watch mode toggle, progress + step line, History sparkline.

## Tokens (gui.py, sampled from the reference)
| Token | Hex | Use | Contrast |
|---|---|---|---|
| SIDE | #131a21 | nav rail | |
| BG | #19212a | page | |
| CARD / RAIL | #1f2731 | cards, strips, summary rail | |
| TILE | #28323d | hover, tooltip | |
| TRACK | #35404b | bar tracks, segmented control | 1.43 vs card |
| LINE | #2a333e | dividers, gridlines | |
| BTN / BTN_HOVER | #1679c7 / #1f88dc | primary button (white text) | 4.57 |
| BTN2 | #35414d | secondary button | |
| TEXT | #e7ebef | primary text | 12.6 on card |
| MUTED | #9aa3ad | secondary text | 5.9 on card |
| LABEL | #8a94a0 | uppercase labels, axes | 4.9 on card |
| ACCENT / ACCENT_2 | #2ea3f7 / #9fd4ff | active nav, live state, data series | 5.5 on card |
| GREEN / AMBER / RED | #3ec4a7 / #eab83a / #ef5f76 | GO / RISKY / DON'T QUEUE, health | 6.9 / 8.2 / 4.7 on card |

## Type
- Segoe UI throughout; numbers in Segoe UI Variable Display (regular weight, never bold); icons in Segoe
  Fluent Icons. Sizes are logical px scaled by the screen DPI (`px()` / `font()`).
- Labels 10 px bold uppercase LABEL. Body 12 px. Small notes 11 px. Numbers 20–24 px. Verdict 22 px semibold.
  View title 18 px.

## Components
- **Nav rail (72 px):** icon + 10 px label; active view = accent icon and a 2 px accent bar on the left edge.
- **Card:** CARD fill, square corners, 18 px padding, uppercase title top-left, optional meta top-right.
- **Strip:** a CARD row on the page background with 4 px gaps (advice items, table rows).
- **Health bar:** 56 px × 3 px, fill = status color, length = score; always followed by Good / Risky / Bad.
- **Mini-stat:** label, number + unit, ▲/▼ change since the last check (green = better, coral = worse).
- **Buttons:** primary blue with white text and a leading icon; secondary slate; 34–36 px tall.

## Rules
- Colour never carries meaning alone: every bar, dot and verdict has a word next to it.
- One primary action per view.
- A live prediction shows the verdict as "Leaning …" in muted regular type until its range is within ±5.
- **Views are drawn on one canvas each (`CanvasPage`).** On Windows every Tk label or frame is a native
  window, and a resize moves and repaints each one (~0.5 ms per widget per frame). Only interactive controls
  (buttons, the dial, the session menu, the duration picker, charts) are real widgets, embedded in the canvas.
  Keep it that way: a view built from hundreds of labels will stutter when the window is resized.
