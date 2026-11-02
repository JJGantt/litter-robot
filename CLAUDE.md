# Working in litter-robot

This watcher presses a physical button on a machine with a cat in it. Two things follow from that.

## Never loosen the gates on an auto-tare

The phantom-weight rescue presses Reset only when the box is **idle**, the scale has read non-zero
**continuously** for `SCALE_STUCK_SECONDS` (1800), and the reading is **essentially static** across
that whole window (`SCALE_STATIC_TOL`). Each gate is load-bearing:

- 1800s is the robot's *own* 30-minute lockout threshold, deliberately matched so the watcher is
  never more aggressive than the manufacturer's "this cannot be a cat". It looks needlessly slow. It
  is not.
- The static check is what separates a drifted zero from an animal: a live cat's weight wanders as it
  shifts, a bad tare is dead flat. If it wanders, the watcher alerts instead of pressing.

A cat spun in the globe is the one outcome this repo exists to prevent. Do not shorten the window,
widen the tolerance, or skip a gate to make a test easier.

The environment can only tighten them: `GATE_FLOORS` stops the watcher at startup (a
`gate_config_error`, reported) when `SCALE_STUCK_SECONDS`, `STALL_CONFIRM_SECONDS` or
`SCALE_STATIC_TOL` is set looser than its default. Changing a floor in the code is the same decision
as loosening the gate.

`check_lockout` presses on a second, narrower case — the robot's own `DC_CAT_DETECT_30M` lockout,
still showing over a scale that reads zero — and it reaches that by *adding* gates, never by dropping
one of the above. The firmware has already served the 30 minutes; a zero reading means there is
nothing on the scale to spin; a non-zero reading is handed back to the auto-tare, which is the path
that alerts rather than presses when the weight wanders. The suffix is the whole safety margin:
`DC_CAT_DETECT` without it is a live cat standing on the scale.

`test_lockout.py` writes each of those gates down as a case and the deploy workflow runs it, so a
gate removed in `check_lockout` fails the build rather than the animal. Run it directly —
`python3 test_lockout.py` — it stubs its imports and needs nothing installed. Add a case there when
you add a gate.

## A stall the robot flagged a cat in is never pressed

`check_stall` presses Reset on a halted cycle. A halted cycle is also exactly what a cat walking in
on a cycle looks like, and the scale gate there cannot see it (see below). The robot's
`isCatDetectPending` rising during the stall is the only cat signal available, so it latches the
episode hands-off. Do not drop that gate or shorten `STALL_CONFIRM_SECONDS` below the time the flag
takes to rise (16s measured) — a 30s press restarted the globe on a cat on 2026-10-23.
`test_stall.py` pins it.

## The robot is held in Sleep Mode on purpose

Solid purple is the normal resting colour here, not a fault. Sleep Mode is what stops the robot
cycling on its own; the watcher fires every clean itself `CLEAN_DWELL_S` after the robot's
`isCatDetectPending` flag reports a visit. Taking the robot out of Sleep Mode in the Whisker app
breaks the model this code is built on, and the watcher will alert about it.

## The cloud never carries the live scale

`weightSensor` in Whisker's state document reads 0.0 with a cat, or 10 lb of iron, on the scale,
asleep or awake (measured 2026-08-29). It is only ever non-zero as a reading stuck after a cycle,
which is all the tare and lockout paths use it for. Do not add a trigger, a gate, or a test that
expects it to show an animal; `test_visit.py` states what the visit trigger may rely on instead.

## Pushing deploys — and a restart costs state

`.github/workflows/deploy-watcher.yml` ships on any push to `main` except one touching only docs
(`*.md`) or tests (`test_*.py`). The Fly deploy token lives in the
repo's `FLY_API_TOKEN` secret, so no machine needs `flyctl` for a normal deploy.

Doc-only pushes deliberately do **not** deploy. The watcher's episode state — a pending clean, a
stall it is part-way through rescuing — lives in memory only, so a restart drops it. Don't redeploy
to "apply" something that isn't code.

A green deploy proves only that Fly accepted the release. The workflow then greps the log for a
`startup` line, because the failure that matters is the watcher not reconnecting to the Whisker API.

## The Whisker API is unofficial and lies quietly

`pylitterbot` is a reverse-engineered client, and `refresh()` has returned stale data with no error —
the watcher was blind for a day. Hence the whole connection being recycled every `RECONNECT_EVERY_S`
and a timeout on every call. Keep both when touching the connection path.

Nothing in `robotStatus` reports a phantom weight; it stays `ROBOT_IDLE`. The light bar is the only
place some states appear, which is why `displayCode` is carried in the snapshot and logged on change.

## Secrets

`.env-litter` holds the Whisker account credentials and is gitignored. In production they are Fly
secrets, set with `flyctl` from a logged-in machine.
