# litter-robot

Auto-resume watcher for the Litter-Robot 4 (Whisker), built on the unofficial
[`pylitterbot`](https://github.com/natekspencer/pylitterbot) API.

## Problem

The robot intermittently throws a false `CAT_SENSOR_INTERRUPTED` mid-cycle —
the weight sensor reads ~0 (no cat), but the clean cycle halts with the globe
open. Resuming requires a manual press of the reset button.

## What it does

Holds a websocket subscription to the robot for instant reaction. When it sees
the box stuck in `CAT_SENSOR_INTERRUPTED` / `PAUSED` **and** the weight sensor
reads ~0 (no cat present), it sends `shortResetPress` to resume the cycle. After
each press the next push/refresh re-evaluates, so it retries — and undoes an
accidental toggle into pause — until the cycle is moving again. A real cat
(weight present) is never touched.

A periodic backstop refresh guarantees it can't sit stuck even if a websocket
push is missed, and it reconnects on any failure. Every event is logged as JSON.

## Config

Environment variables (see `.env-litter`, not committed):

| Var | Default | Meaning |
|-----|---------|---------|
| `LITTER_USER` / `LITTER_PASS` | — | Whisker account credentials (required) |
| `BACKSTOP_SECONDS` | 20 | Safety-net refresh interval |
| `WEIGHT_THRESHOLD_LB` | 3.0 | Max weight treated as "no cat" |
| `RESET_CAP` | 15 | Max presses per stuck incident |
| `LOG_DIR` | ./logs | Log directory |

## Run

```
pip install -r requirements.txt
python litter_watcher.py
```

## Deploy (Fly.io)

Runs as an always-on worker (no public ports). Credentials live in Fly secrets:

```
fly secrets set LITTER_USER=... LITTER_PASS=...
fly deploy
fly logs
```
