# litter-robot

Auto-resume watcher for the Litter-Robot 4 (Whisker), built on the unofficial
[`pylitterbot`](https://github.com/natekspencer/pylitterbot) API. It runs as an always-on
Fly worker and drives the box: it keeps the robot in Sleep Mode so the robot never cycles on
its own, fires the clean cycle itself after a visit, rescues the three ways the box gets stuck,
mirrors status to Supabase for the status hub, and colours a Govee closet light with what it
is doing.

## The three failures it fixes

All three come from the same flaky part — the LR4's weight scale, and they fail in opposite
directions, which is why each needs its own detector and why none of them can cover another.

**1. A stall mid-cycle (empty scale).** The robot throws a false `CAT_SENSOR_INTERRUPTED`
partway through a clean cycle: the scale reads ~0 (no cat), but the globe halts, open.
The watcher confirms the scale is empty and has stayed empty for a dwell, then sends
`shortResetPress` to resume, retrying on a spaced cadence until the cycle moves again. A real
cat (weight present) is never touched. This happens *a lot* — 70+ presses in a recent 9-day
window.

**2. A phantom weight on an idle box (loaded scale).** The scale drifts off zero — a nudge, a
foot caught on the mat, litter added, the drawer emptied — and the robot then believes a cat
is standing in it. `robotStatus` stays `ROBOT_IDLE`, so *nothing in `status` ever says
anything is wrong*, and the first detector can't see it: it wants a stalled cycle with an
empty scale, and this is a parked globe with a loaded one. After 30 minutes the robot gives up
and locks itself out — **blue light bar with partial yellow flashing — and it stops cycling
until someone presses Reset.** Whisker's own fix for that light code is a short reset press,
which re-zeroes the scale, so the watcher does it.

The gates before it presses, so a real cat is never spun: the box must be idle (a running or
stalled cycle belongs to detector 1), the scale must read non-zero *continuously* for
`SCALE_STUCK_SECONDS` (default 1800 — the robot's own 30 minutes, so we are never more
aggressive than the manufacturer's "this cannot be a cat"), and the reading must be
essentially **static** across that window. A live animal's weight wanders as it shifts; a
drifted zero is dead flat. If it wanders, the watcher does not press — it alerts instead, so
a cat that has genuinely settled in for the afternoon is safe.

If several tare presses fail to zero it, that is the symptom Whisker replaces the base unit
for under WhiskerCare, and the alert says so.

**3. A lockout still showing after the scale has come back to zero.** The same 30-minute
lockout as failure 2, outliving the weight that caused it. Detector 2 cannot end this one: it
starts its dwell on a *non-zero* reading, and the scale now reads 0.0, so it sees a clean idle
box and never begins. Detector 1 wants a stalled mid-cycle status, and this one is `READY`. The
box sits dead with nothing looking at it — 80 minutes of it on 2026-08-27, and it would have
stayed that way until a human noticed the light bar.

The watcher presses Reset once `DC_CAT_DETECT_30M` has held for `LOCKOUT_CONFIRM_SECONDS`
(default 120) on an idle box whose scale reads zero. The suffix carries the whole meaning:
`DC_CAT_DETECT_30M` is the lockout, while plain `DC_CAT_DETECT` is an ordinary cat standing on
the scale and is never acted on.

This is a **narrower** case than detector 2, not a looser one. The robot's own firmware has
already spent its 30 minutes deciding this cannot be an animal, and a scale reading zero means
there is nothing on it to spin; the globe is parked, so the press is a tare and not a cycle. A
non-zero reading is handed straight back to detector 2 — the path that alerts instead of
pressing when the weight wanders, which is what keeps a cat that has settled in for the
afternoon safe. `test_lockout.py` states each of those gates as a case, and the deploy workflow
runs it before it ships.

## Litter-Robot 4 light codes worth knowing

The panel is the only place some of these states are ever reported. The full list is in
Whisker's [light codes guide](https://www.litter-robot.com/support/article/litter-robot-4-light-codes-guide/).

| Light bar | Means | Notes |
|---|---|---|
| **Solid purple** | Sleep Mode active | This is the normal resting colour here — the watcher deliberately keeps the robot asleep and fires cleans itself |
| **Blue, partial yellow flashing** | Scale triggered 30+ min (`DC_CAT_DETECT_30M`) | Failures 2 and 3 above. The box will not cycle. Reset zeroes the scale |
| **Blue flashing** | Waste drawer full | Empty it, press Reset |
| **Blue with one yellow blink by the Connect button** | Wi-Fi lost / onboarding | Not a reset fix — re-run Update Network in the Whisker app |
| **Solid yellow** | Paused | |
| **Yellow round-robin** | Clean or Empty cycle running | |
| **Solid red** | Cat detected in the globe | |
| **Red, flashing yellow** | Globe jammed or overloaded | Motor stops |
| **Red and blue alternating** | The three laser curtain sensors need cleaning | |

## What else it does

- **Drives cleaning.** The robot is held in Sleep Mode (set once in the Whisker app) so it
  never auto-cycles. A cat visit marks the box dirty; once the scale has been empty for
  `CLEAN_DWELL_S` the watcher starts the cycle itself. It refuses to cycle into a genuinely
  full drawer, and resumes the moment the drawer is emptied.
- **Failsafes.** Alerts to the phone (APNs via the status hub) if the robot
  goes unreachable, drops out of sleep mode, fills its drawer, or gets a scale it cannot
  zero. A healthchecks.io ping, fed once per connection cycle, catches a dead watcher.
- **Survives a half-open socket.** A stale `refresh()` that returns old data with no error
  once left the watcher silently blind for a day, so the whole Whisker connection is recycled
  every `RECONNECT_EVERY_S` and every call has a timeout.
- **Publishes status** to Supabase `device_status` for the hub's litter card, at least every
  `PUBLISH_SECONDS` — not only on a state change, which used to leave the card reading hours
  stale through any quiet stretch.
- **Durable log.** Every meaningful event is mirrored to Supabase `litter_events` (the
  on-disk log is wiped by each Fly redeploy). The robot's raw `displayCode` — the light bar —
  is carried in the snapshot and logged on change, so what the panel was showing is
  recoverable after the fact.

## Config

Environment variables (see `.env-litter`, not committed). The safety-critical ones are documented in
[CLAUDE.md](CLAUDE.md) — do not retune those to make something convenient.

| Var | Default | Meaning |
|-----|---------|---------|
| `LITTER_USER` / `LITTER_PASS` | — | Whisker account credentials (required) |
| `SLOW_POLL_SECONDS` | 20 | Idle baseline poll interval (5 in `fly.toml`) |
| `FAST_POLL_SECONDS` | 1 | Poll interval while a cycle is active |
| `MAX_FAST_SECONDS` | 600 | Stop fast-polling after this long |
| `WEIGHT_THRESHOLD_LB` | 3.0 | Max weight treated as "no cat" |
| `RESET_RETRY_SECONDS` | 30 | Min gap between reset presses on a stall |
| `STALL_CONFIRM_SECONDS` | 30 | Stall must hold an empty scale this long before the first press |
| `SCALE_ZERO_TOL` | 0.2 | \|lb\| below this counts as a zeroed scale |
| `SCALE_STUCK_SECONDS` | 1800 | Non-zero scale on an idle box this long → auto-tare |
| `SCALE_STATIC_TOL` | 0.25 | Max peak-to-peak lb across that window to still call it a drift |
| `SCALE_RETRY_SECONDS` | 120 | Min gap between tare presses |
| `SCALE_ALERT_AFTER` | 3 | Failed tare presses before alerting a human |
| `DRIVE_CLEANING` | 1 | Master switch for firing cleans ourselves; off leaves the box idle |
| `CLEAN_DWELL_S` | 900 | Quiet time after the last visit before we fire a clean |
| `CLEAN_MIN_GAP_S` | 120 | Never re-fire a clean within this of the last one |
| `STUCK_ALERT_SECONDS` / `STUCK_REALERT_SECONDS` | 600 / 1800 | First alert on a stall, then how often to remind |
| `OFFLINE_ALERT_SECONDS` | 180 | Robot unreachable this long → high-priority alert |
| `DRAWER_FULL_PCT` / `LITTER_LOW_PCT` | 90 / 75 | Thresholds behind the drawer-full and litter-low alerts |
| `PUBLISH_SECONDS` | 600 | Refresh `device_status` at least this often |
| `RECONNECT_EVERY_S` | 600 | Recycle the Whisker connection this often |
| `LOG_DIR` | ./logs | Log directory |

## Run

```
pip install -r requirements.txt
python litter_watcher.py
```

## Deploy (Fly.io)

Runs as an always-on worker (no public ports). Credentials live in Fly secrets.

**Shipping is CI's job.** Pushing a change to `litter_watcher.py`, `requirements.txt`, `Dockerfile`
or `fly.toml` on `main` runs `.github/workflows/deploy-watcher.yml`, which deploys to Fly and then
checks the log for a `startup` line — a green deploy only proves Fly accepted the release, not that
the watcher reconnected to the Whisker API. Trigger it by hand with:

```
gh workflow run deploy-watcher.yml -R JJGantt/litter-robot
```

That exists so a fix does not wait on a particular laptop being awake: the Fly deploy token lives in
the repo's `FLY_API_TOKEN` secret (app-scoped, minted with `fly tokens create deploy`), so any machine
that can push can ship. Neither the Pi nor the Mac needs `flyctl` for a normal deploy.

Doc-only pushes deliberately do **not** deploy — a restart drops the watcher's in-memory episode
state (a pending clean, a stall it is part-way through rescuing).

Secrets are still set with flyctl from a logged-in machine:

```
fly secrets set LITTER_USER=... LITTER_PASS=...
fly logs
```
