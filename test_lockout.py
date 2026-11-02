#!/usr/bin/env python3
"""Executable statement of the gates on check_lockout, which presses a physical button.

Run it with plain `python3 test_lockout.py` — the deploy workflow does, before it ships. The three
third-party imports are stubbed so this needs nothing installed; it exercises the decision, not the
Whisker client.

Each case below is a way this could press on a live animal, written down so removing a gate fails
here instead of in the globe. If a change makes one of these red, the change is wrong.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="lockout-test-")

for name, attrs in (("httpx", {"AsyncClient": object, "Timeout": object}),
                    ("dotenv", {"load_dotenv": lambda *a, **k: None}),
                    ("pylitterbot", {"Account": object})):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod

sys.path.insert(0, str(HERE))
import litter_watcher as lw   # noqa: E402


class FakeRobot:
    """Counts presses instead of making one."""
    def __init__(self):
        self.presses = 0

    async def reset(self):
        self.presses += 1


def snap(code="DC_CAT_DETECT_30M", status="READY", weight=0.0):
    return {"status": status, "cycleState": None, "cycleStatus": None, "weight": weight,
            "catDetect": None, "drawerPct": 58, "litterPct": 0.9, "online": True,
            "is_sleeping": True, "catWeight": 11.84, "displayCode": code}


def steady(s, t0, t1, step=20):
    """The same snapshot on every poll from t0 to t1 (seconds), at the idle poll cadence."""
    return [(t, s) for t in range(t0, t1, step)]


async def run(frames):
    w = lw.Watcher(FakeRobot())
    for t, s in frames:
        await w.check_lockout(s, t)
    return w.robot.presses, w


async def main() -> int:
    failed = []

    def check(name, got, want):
        if got == want:
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}: got {got!r}, want {want!r}")
            failed.append(name)

    # The failure this exists for: 2026-08-27, 80 minutes locked out on an idle box. The cloud weight
    # reads 0.0 here as it does everywhere, cat or no cat, so check_scale never starts on it.
    presses, w = await run(steady(snap(), 0, 400))
    check("presses on a confirmed lockout on an idle box", presses, 1)
    check("and flags the closet light while it holds", w.lockout_flagged, True)

    # Presses are spaced by LOCKOUT_RETRY_SECONDS, not issued once per poll.
    presses, _ = await run(steady(snap(), 0, 1000))
    check("retries are spaced, not one per poll", presses, 3)

    # DC_CAT_DETECT is an ordinary cat on the scale and differs by one suffix. Never press.
    presses, _ = await run(steady(snap(code="DC_CAT_DETECT"), 0, 4000))
    check("an ordinary cat-detect is never pressed", presses, 0)

    # The cloud has never published a non-zero weight, but if it does, that reading is check_scale's
    # episode — the path that alerts instead of pressing when the weight wanders.
    presses, _ = await run(steady(snap(weight=8.4), 0, 4000))
    check("a non-zero weight defers to check_scale", presses, 0)

    # A parked globe is why a press here clears the latch without a cycle. Mid-cycle belongs to the
    # stall watcher.
    presses, _ = await run(steady(snap(status="CAT_SENSOR_INTERRUPTED"), 0, 4000))
    check("a mid-cycle status is not ours to press", presses, 0)

    # The Whisker API serves stale snapshots without error, so a code already being left is not a press.
    presses, _ = await run(steady(snap(), 0, 100) + steady(snap(code="DC_MODE_IDLE"), 100, 4000))
    check("a lockout that clears inside the dwell is not pressed", presses, 0)

    # A snapshot with no weight field is an incomplete read; hold rather than press on it.
    presses, _ = await run(steady(snap(weight=None), 0, 4000))
    check("a null weight is not pressed", presses, 0)

    # Going offline ends the episode (evaluate calls _end_lockout_episode on that branch). The dwell
    # has to restart on the way back: carried across an outage it would already be satisfied, and the
    # first snapshot to return — the stale read the dwell exists to catch — would earn a press.
    w = lw.Watcher(FakeRobot())
    for t, s in steady(snap(), 0, 100):
        await w.check_lockout(s, t)
    w._end_lockout_episode()                                  # what the offline branch does
    for t, s in steady(snap(), 100, 200):                     # back, still locked, 100s into a new dwell
        await w.check_lockout(s, t)
    check("an outage restarts the dwell", w.robot.presses, 0)
    for t, s in steady(snap(), 200, 260):                     # and it presses once the new dwell is served
        await w.check_lockout(s, t)
    check("and presses once that dwell is served", w.robot.presses, 1)

    # Clearing ends the episode, so a later lockout serves its own dwell rather than pressing at once.
    presses, _ = await run(steady(snap(), 0, 400)
                           + steady(snap(code="DC_MODE_IDLE"), 400, 500)
                           + steady(snap(), 500, 900))
    check("a second episode starts a fresh dwell", presses, 2)

    print("\n" + ("all gates hold" if not failed else f"GATES BROKEN: {failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
