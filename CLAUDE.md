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

## The robot is held in Sleep Mode on purpose

Solid purple is the normal resting colour here, not a fault. Sleep Mode is what stops the robot
cycling on its own; the watcher fires every clean itself once the scale has been empty for
`CLEAN_DWELL_S`. Taking the robot out of Sleep Mode in the Whisker app breaks the model this code is
built on, and the watcher will alert about it.

## Pushing deploys — and a restart costs state

`.github/workflows/deploy-watcher.yml` ships on any push to `main` that touches `litter_watcher.py`,
`requirements.txt`, `Dockerfile`, `fly.toml`, or the workflow. The Fly deploy token lives in the
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
