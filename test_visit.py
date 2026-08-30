#!/usr/bin/env python3
"""Executable statement of what the visit trigger in maybe_clean may rely on, and when it may fire.

Run it with plain `python3 test_visit.py` — the deploy workflow does. Third-party imports are stubbed
so this needs nothing installed; it exercises the decision, not the Whisker client.

The robot publishes exactly one real-time sign of a visit while asleep: isCatDetectPending. The live
scale is never published (0.0 with 10 lb on it, 2026-08-29) and the recorded pet weight skips
visits. Each case below pins one of those facts so a change that quietly re-trusts the scale, or
fires against the robot's own verdict, fails here instead of in the closet.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="visit-test-")

for name, attrs in (("httpx", {"AsyncClient": object, "Timeout": object}),
                    ("dotenv", {"load_dotenv": lambda *a, **k: None}),
                    ("pylitterbot", {"Account": object})):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod

sys.path.insert(0, str(HERE))
import litter_watcher as lw   # noqa: E402

DWELL = int(lw.CLEAN_DWELL_S)


class FakeRobot:
    """Counts cleans instead of starting one."""
    def __init__(self):
        self.cleans = 0

    async def start_cleaning(self):
        self.cleans += 1


def snap(pending=False, status="READY", sleeping=True, drawer=20, cat_weight=12.49, weight=0.0):
    return {"status": status, "cycleState": None, "cycleStatus": None, "weight": weight,
            "catDetect": None, "drawerPct": drawer, "litterPct": 0.9, "online": True,
            "is_sleeping": sleeping, "catWeight": cat_weight, "catDetectPending": pending,
            "displayCode": "DC_MODE_IDLE"}


def steady(s, t0, t1, step=5):
    """The same snapshot on every poll from t0 to t1 (seconds), at the idle poll cadence."""
    return [(t, s) for t in range(t0, t1, step)]


async def run(frames, w=None):
    w = w or lw.Watcher(FakeRobot())
    for t, s in frames:
        await w.maybe_clean(s, t)
    return w.robot.cleans, w


async def main() -> int:
    failed = []

    def check(name, got, want):
        if got == want:
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}: got {got!r}, want {want!r}")
            failed.append(name)

    # The visit: the flag rises. Nothing fires inside the dwell, one clean fires after it.
    idle = steady(snap(), 0, 60)
    visit = steady(snap(pending=True), 60, 60 + DWELL - 5)
    cleans, w = await run(idle + visit)
    check("a visit does not fire inside the dwell", cleans, 0)
    cleans, w = await run(steady(snap(pending=True), 60 + DWELL - 5, 60 + DWELL + 30), w)
    check("and fires once the dwell is served", cleans, 1)

    # The robot clears the flag when the cycle starts; that is the reset, not our own bookkeeping.
    cleans, w = await run(steady(snap(pending=False), 60 + DWELL + 30, 60 + DWELL + 600), w)
    check("the falling edge ends the episode", cleans, 1)
    cleans, w = await run(steady(snap(pending=True), 60 + DWELL + 600, 60 + DWELL + 600 + DWELL + 30), w)
    check("and the next rise is a new visit", cleans, 2)

    # A button press mid-dwell clears the flag: nothing is owed, so nothing fires at the dwell.
    cleans, _ = await run(steady(snap(), 0, 60) + steady(snap(pending=True), 60, 360)
                          + steady(snap(pending=False), 360, 60 + DWELL + 600))
    check("a cycle someone else ran cancels the pending clean", cleans, 0)

    # The scale is never published. 10 lb on it reads 0.0; a reading that did appear is a stuck tare,
    # not a cat, and must neither mark a visit nor fire a clean.
    cleans, _ = await run(steady(snap(weight=10.0), 0, DWELL + 600))
    check("a weight reading alone is not a visit", cleans, 0)

    # A process that starts with the flag already true (redeploy mid-dwell) owes a clean of unknown
    # age: it serves a full dwell from startup, then fires.
    cleans, w = await run(steady(snap(pending=True), 0, DWELL - 5))
    check("a visit already owed at startup waits a full dwell", cleans, 0)
    cleans, _ = await run(steady(snap(pending=True), DWELL - 5, DWELL + 30), w)
    check("and then fires", cleans, 1)

    # Awake, the robot runs its own countdown and clears its own flag. Never fire on top of it.
    cleans, _ = await run(steady(snap(pending=True, sleeping=False), 0, DWELL + 600))
    check("never fires while the robot is awake", cleans, 0)

    # A full drawer holds the clean; emptying it releases the clean that was owed.
    cleans, w = await run(steady(snap(pending=True, drawer=95), 0, DWELL + 600))
    check("a full drawer holds the clean", cleans, 0)
    cleans, _ = await run(steady(snap(pending=True, drawer=0), DWELL + 600, DWELL + 700), w)
    check("emptying it releases the clean", cleans, 1)

    # The recorded pet weight is secondary. With the flag up it restarts the dwell (a second cat
    # inside the window); with the flag down it is a late score for a cycle already run.
    frames = (steady(snap(), 0, 60) + steady(snap(pending=True), 60, 600)
              + steady(snap(pending=True, cat_weight=14.93), 600, 60 + DWELL + 30))
    cleans, w = await run(frames)
    check("a weight record inside the window restarts the dwell", cleans, 0)
    cleans, _ = await run(steady(snap(pending=True, cat_weight=14.93), 60 + DWELL + 30, 600 + DWELL + 30), w)
    check("and the clean fires from the record, not the flag", cleans, 1)
    cleans, _ = await run(steady(snap(), 0, 60) + steady(snap(cat_weight=14.93), 60, DWELL + 600))
    check("a weight record against a cleared flag does not fire", cleans, 0)

    # Only an idle box is cycled; mid-cycle states belong to the stall watcher.
    cleans, _ = await run(steady(snap(pending=True, status="CAT_SENSOR_INTERRUPTED"), 0, DWELL + 600))
    check("a mid-cycle status is not cycled", cleans, 0)

    print("\n" + ("all gates hold" if not failed else f"GATES BROKEN: {failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
