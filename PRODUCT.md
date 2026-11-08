# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

<!-- Native Windows desktop app built with CustomTkinter (Tk). It uses no iOS/Android design language; "web" is the closest schema value. -->

## Users
One person: the owner, a competitive online gamer who checks their own connection before queueing ranked matches, and sometimes leaves a day-long monitor running to collect evidence about a flaky network.

## Product Purpose
MatchReady answers one question in about 30 seconds: is my connection good enough to queue a competitive match right now? It gives a 0–100 score and a verdict, GO (80+), RISKY (60–79) or DON'T QUEUE (<60), explains what's hurting the connection, and forecasts the next 30 minutes from past checks. Success means a confident yes/no before queueing, and a report solid enough to hand to whoever maintains the network.

## Positioning
A speed test measures bandwidth, which games barely use. MatchReady weighs what games actually suffer from (packet loss, jitter and lag spikes, ping, bufferbloat, UDP health, Wi-Fi quality) and turns them into a decision, not a pile of numbers.

## Operating Context
- Run on the gaming PC right before queueing, often with the game already open; the decision has to read at a glance.
- Live view while a check runs: predicted score with a ± range, live ping graph, step checklist.
- Watch mode re-checks every 2 minutes and alerts on drops.
- Monitor tab: 1/8/24 h runs, then a plain-language report (.txt + .csv) for a maintenance request.
- History tab: scores over time and typical score by hour of day.

## Capabilities and Constraints
- CustomTkinter + Tk canvases + Pillow only; no matplotlib/numpy (a test guards this, exe stays ~15 MB).
- The app watches its own source files and rebuilds itself while open.
- Keyboard: Ctrl+R / F5 run a check, Ctrl+1–4 switch views, Esc dismisses alerts.
- Measures nearby game datacenters, not the game's actual match servers; in-game ping is usually within ±10 ms.

## Brand Commitments
- The speedometer-style score dial is a keeper (user decision, 2026-11-06).
- Verdict vocabulary: GO / RISKY / DON'T QUEUE.

## Evidence on Hand
Real measurements only, from the user's own checks (history in %APPDATA%\MatchReady\history.db). No testimonials, benchmarks or claims beyond what the app measures.

## Product Principles
1. The decision comes first; numbers support it.
2. Never show a stale or partial result as if it were current.
3. Explain the cause in plain language, then the fix.
4. Colour never carries meaning alone; every state has a word.
