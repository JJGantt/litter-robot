#!/usr/bin/env python3
"""Executable statement of the gates on check_stall, which presses Reset on a halted cycle.

Run it with plain `python3 test_stall.py` — the deploy workflow does, before it ships. Third-party
imports are stubbed so this needs nothing installed; it exercises the decision, not the Whisker client.

A halted cycle is usually a false cat-sensor trip that needs a press, but it is also exactly what the
robot does when a cat walks in on a cycle. The live scale never reaches the cloud, so the only sign of
the cat is the robot raising isCatDetectPending mid-cycle. Each case pins one way of pressing on it.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="stall-test-")

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


async def no_notify(*a, **k):
    return None


lw.notify = no_notify


def snap(status="CAT_SENSOR_INTERRUPTED", pending=False, weight=0.0):
    return {"status": status, "cycleState": "CYCLE_STATE_CAT_DETECT", "cycleStatus": "CYCLE_DUMP",
            "weight": weight, "catDetect": "CAT_DETECT_CLEAR", "drawerPct": 50, "litterPct": 90,
            "online": True, "is_sleeping": True, "catWeight": 12.0, "catDetectPending": pending,
            "displayCode": "DC_CAT_DETECT"}


def steady(s, t0, t1, step=1):
    """The same snapshot on every poll from t0 to t1 (seconds), at the fast-poll cadence of a cycle."""
    return [(t, s) for t in range(t0, t1, step)]


async def run(frames, w=None):
    w = w or lw.Watcher(FakeRobot())
    for t, s in frames:
        await w.check_stall(s, t)
    return w.robot.presses, w


async def main() -> int:
    failed = []

    def check(name, got, want):
        ok = got == want
        print(f"{'ok  ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
        if not ok:
            failed.append(name)

    # A false trip: halted, pending never rises. This is the case the press exists for.
    presses, _ = await run(steady(snap(), 0, 70))
    check("a stall with no cat flagged is pressed once the dwell is served", presses, 1)

    presses, _ = await run(steady(snap(), 0, 50))
    check("nothing is pressed inside the dwell", presses, 0)

    # 2026-10-23: the cycle starts with a cat in the globe, the robot halts, and raises the pending flag
    # 16s later. A 30s press restarted the globe on the cat.
    presses, w = await run(steady(snap(), 0, 16) + steady(snap(pending=True), 16, 4000))
    check("the robot flagging a cat mid-stall is never pressed", presses, 0)
    check("and the episode is latched as a cat", w.cat_in_stall, True)

    # The flag can drop again while the globe stays halted; the cat may still be there.
    presses, _ = await run(steady(snap(), 0, 16) + steady(snap(pending=True), 16, 20)
                           + steady(snap(), 20, 4000))
    check("the latch survives the flag dropping inside the same stall", presses, 0)

    presses, _ = await run(steady(snap(pending=True), 0, 4000))
    check("a stall that begins with the flag up is never pressed", presses, 0)

    # The robot resumes on its own once the cat has gone; the next stall is judged from scratch.
    presses, w = await run(steady(snap(pending=True), 0, 30) + steady(snap(status="CLEAN_CYCLE"), 30, 40)
                           + steady(snap(), 40, 110))
    check("a fresh stall after the robot resumed is judged on its own", presses, 1)

    presses, _ = await run(steady(snap(weight=8.0), 0, 4000))
    check("a weight on the scale is never pressed", presses, 0)

    presses, _ = await run(steady(snap(status="READY"), 0, 4000))
    check("a box that is not stalled is never pressed", presses, 0)

    print("all passed" if not failed else f"{len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
