# MatchReady

A Windows app that checks whether your connection is good enough for **online gaming** *before* you
queue a competitive match. Any fast-paced online game (shooters, fighting, sports, racing) suffers from the
same things, so one ~30-second check gives a single 0–100 score and a verdict: **GO** (80+), **RISKY**
(60–79) or **DON'T QUEUE** (<60). It also explains what's hurting you and forecasts the next 30 minutes
from your history.

## Run it
- **Exe:** `dist\MatchReady.exe` (build it with `build.bat`)
- **From source:** `run.bat`, or `.venv\Scripts\python.exe MatchReady.py`

Tip: run a check when the network is good and when it's bad, at different times of day. The forecast and the
History tab ("Typical score by hour") get more accurate with more data. **Watch mode** re-checks every
2 minutes, logs the results, and pops an alert if the score drops below RISKY.

## Day-long monitor
The **Monitor** tab checks the connection every minute for 1, 8 or 24 hours (plus a full check with a speed
test every 3 hours) and logs every result. It then reports:
- how much of the time the connection was usable, every problem period and every complete dropout
- the hours of the day when it gets worse (busy-hour congestion) vs. random drops (an intermittent fault)
- whether the fault is local (the router also struggles: Wi-Fi, access point, cable) or past the router
  (building/campus network or internet provider), plus Wi-Fi signal and access-point switching
- whether the results justify a maintenance request, and who should get it

**Save report** writes a plain-language summary (`.txt`) and every measurement (`.csv`) to
`Documents\MatchReady reports`, ready to attach to a ticket; it also saves automatically when a monitor ends.
The PC is kept awake while it runs, and if MatchReady closes mid-run it resumes on the next launch.

## What it measures
| Factor | Why it matters |
|---|---|
| Ping to nearby game datacenters (the nearest one counts) | Delay before your inputs reach the server |
| Jitter and lag spikes | Unstable ping causes rubber-banding (very noticeable in RL) |
| Packet loss and dropouts | Lost inputs, ghost hits, disconnects |
| UDP via STUN, NAT type | Games use UDP; some networks throttle or block it |
| Bufferbloat (ping during a speed test) | How much lag spikes when the network is busy |
| Download / upload | Only a floor check; games use < 1 Mbps |
| Router ping | Separates "my Wi-Fi is bad" from "the campus/ISP is bad" |
| Wi-Fi signal, band, channel crowding, link rate | Weak or 2.4 GHz Wi-Fi is the most common cause of loss and jitter |
| DNS, captive portal, VPN, CPU, background traffic | Things that quietly ruin a match |

### How the score is weighted
| Factor | Weight | Why |
|---|---|---|
| Packet loss | 28% | Lost inputs, ghost hits and rubber-banding in every genre |
| Jitter and lag spikes | 24% | Games smooth over steady ping, not swings |
| Ping | 24% | Under ~40 ms is perfect; it gets noticeable past ~80 ms |
| Bufferbloat | 12% | Lag when someone else on the network is downloading or streaming |
| UDP health | 7% | Almost all games send their gameplay traffic over UDP |
| Wi-Fi quality | 5% | Signal, band and channel crowding (Ethernet gets full marks) |

A weak core factor (ping, jitter, loss, UDP) drags the whole score down, and loss over 2%, dropouts or
blocked UDP cap it. See `matchready/scoring.py` to tune it.

## The dial
The score shows on a speedometer-style dial (red / amber / green zones), with Ping, Jitter, Download and Upload underneath, like a speed test. Before your first check the dial is a big **GO** button: click it (or press **Run check** / Ctrl+R) to start.

## Live results while a check runs
You don't wait the full ~30 seconds to see something. About a second after you press **Run check**:
- the dial shows a **predicted score with a ± range** (shaded band) (labelled "Live prediction"). The range narrows as UDP and
  the speed test finish, and the final score replaces it;
- a **live graph** draws every ping as it arrives (red ticks are lost packets), and switches to "ping while
  downloading/uploading" during the load test so you can watch bufferbloat happen;
- a **checklist** shows what the check is doing right now, with live numbers;
- the measurements fill in as they're taken.

Early loss is damped (one dropped ping in the first second is not "2% loss"), and until UDP and bufferbloat are
measured the prediction borrows them from your last check on the same connection. If the speed test is blocked
or rate-limited, it's reported as "Unavailable" rather than 0 Mbps and doesn't affect the score.

## Auto-update while you develop
While MatchReady is open, it watches the project's source files (`matchready\*.py`, `targets.json`,
`MatchReady.py`, `requirements.txt`):
1. Save a change, and a blue banner says **Changes detected**.
2. A couple of seconds after you stop saving, it runs the tests and rebuilds the exe in the background
   (`build.py` → `build\stage`). The banner shows the step and elapsed time; a build takes about 20 seconds
   in total (most of that is PyInstaller; the exe is ~15 MB because the app avoids heavy libraries like
   matplotlib and numpy. Importing them would add a lot to every build, and a test guards against that).
3. **Update ready → Restart now** closes the app, swaps in the new exe and reopens it.
   If a check is running, it waits for it to finish first.

If the tests fail, the banner turns red and you keep the working version. **View log** opens
`build\update.log`. Fix the code and save again to retry. Running from source (`run.bat`), it skips the
build and offers **Restart now** directly. `build.bat` and the app never build at the same time.

## Updates for everyone else (GitHub Releases)
Copies of `MatchReady.exe` you give to other people update themselves from the project's GitHub Releases:
about 8 seconds after launch (and every 6 hours while open) the app checks for a newer version. If there is
one, a banner offers **Update now**, which downloads it, checks its SHA-256 and restarts into it. A download
that doesn't match is thrown away. If the exe sits somewhere the app can't write to (like Program Files), the
banner links to the release page instead. Your own dev build skips this and keeps updating from source.

**Setup (once):** put the repository in `matchready\release.json`, e.g. `{"repo": "your-name/MatchReady"}`.
The repository must be public so the app can read releases without a password.

**Publish a version:**
```
release.bat 1.1.0 "Smoother charts"
```
This sets the version, runs the tests, builds the exe and creates the GitHub release with `MatchReady.exe`
and `MatchReady.exe.sha256` attached. Uploading needs a GitHub token in `GITHUB_TOKEN` (fine-grained,
"Contents: read and write" on that repo) or the GitHub CLI signed in; without either it builds the files
and prints the steps to upload them on github.com. The exe isn't code-signed, so Windows SmartScreen may
warn on first run of a downloaded copy.

## Configuration
`targets.json` (next to the exe, or `matchready/targets.json` from source) lists the game datacenters to test
(by default Chicago, Dallas and Iowa) and the region name. Edit it to change region. Older per-game files
still work; their servers are merged into one list. History is saved in `%APPDATA%\MatchReady\history.db`
(older two-score history is kept, using the worse of the two scores).

## Limits
Most games don't publish their match-server IPs, so the app measures datacenters in the same areas
(Chicago, Dallas and Iowa by default). In-game ping is usually within ±10 ms.

## Development
```
.venv\Scripts\python.exe -m pytest -q tests
```
