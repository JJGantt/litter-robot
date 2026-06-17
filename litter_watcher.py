#!/usr/bin/env python3
"""Litter-Robot 4 auto-resume watcher.

Holds a websocket subscription to the robot for instant reaction. When it gets
stuck in a false CAT_SENSOR_INTERRUPTED / PAUSED state (weight sensor ~0, i.e.
no cat present), it presses the reset button (shortResetPress) to resume the
cycle. After each press the next push/refresh re-evaluates, so it naturally
retries (and un-does an accidental toggle into PAUSE) until the cycle is moving
again. A real cat (weight present) is never touched.

Reliability: the websocket gives instant response, and a periodic backstop
refresh guarantees it can never sit stuck even if a push is missed or the
socket drops. On any connection failure it reconnects.

Logging: one JSON line per event to stdout (captured by `fly logs`) and to a
daily file under LOG_DIR.

Config (env, with defaults):
  LITTER_USER / LITTER_PASS  - Whisker account credentials (required)
  BACKSTOP_SECONDS           - safety-net refresh interval (default 20)
  WEIGHT_THRESHOLD_LB        - max weight treated as "no cat" (default 3.0)
  RESET_CAP                  - max presses per stuck incident (default 15)
  LOG_DIR                    - log directory (default ./logs)
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pylitterbot import Account
from pylitterbot.event import EVENT_UPDATE

# --- Fix pylitterbot's LR4 realtime websocket ---
# pylitterbot opens the AWS AppSync websocket but never sends the `connection_init`
# handshake, so AppSync never returns connection_ack/start_ack and the subscription
# silently delivers ZERO pushes (everything falls back to polling). Injecting
# connection_init into the WS config makes the monitor send it before subscribing,
# which completes the handshake and turns on real-time updates.
try:
    from pylitterbot.robot.litterrobot4 import LitterRobot4
    _LR4_PROTO = LitterRobot4._WS_PROTOCOL
    _orig_ws_config_factory = _LR4_PROTO.ws_config_factory

    async def _ws_config_factory_with_init(robot):
        cfg = await _orig_ws_config_factory(robot)
        cfg["connection_init"] = {"type": "connection_init"}
        return cfg

    _LR4_PROTO.ws_config_factory = _ws_config_factory_with_init
except Exception:  # never let the patch break startup
    pass

BASE = Path(__file__).parent
load_dotenv(BASE / ".env-litter")

BACKSTOP = float(os.environ.get("BACKSTOP_SECONDS", 20))
WEIGHT_THRESHOLD = float(os.environ.get("WEIGHT_THRESHOLD_LB", 3.0))
RESET_CAP = int(os.environ.get("RESET_CAP", 15))
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Failsafe / alerting
OFFLINE_ALERT_SECONDS = float(os.environ.get("OFFLINE_ALERT_SECONDS", 300))   # robot offline this long -> Telegram
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", 1800))          # ping healthchecks this often
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HEALTHCHECKS_URL = os.environ.get("HEALTHCHECKS_URL", "")
# A clean cycle was in progress when it dropped -> worth calling out in the alert.
MID_CYCLE_STATES = {"CLEAN_CYCLE", "CAT_SENSOR_INTERRUPTED", "PAUSED"}

STUCK_STATES = {"CAT_SENSOR_INTERRUPTED", "PAUSED"}
HEALTHY_STATES = {"CLEAN_CYCLE", "READY", "CLEAN_CYCLE_COMPLETE", "CAT_SENSOR_TIMING", "OFF"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_event(event: str, **fields) -> None:
    rec = {"ts": now_iso(), "event": event, **fields}
    print(json.dumps(rec), flush=True)
    try:
        with (LOG_DIR / f"litter-{datetime.now().strftime('%Y-%m-%d')}.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # ephemeral fs (e.g. no volume mounted) -> stdout still has it


def snapshot(robot) -> dict:
    d = getattr(robot, "_data", {})
    return {
        "status": robot.status.name,
        "cycleState": d.get("robotCycleState"),
        "cycleStatus": d.get("robotCycleStatus"),
        "weight": d.get("weightSensor"),
        "catDetect": d.get("catDetect"),
        "drawerPct": d.get("DFILevelPercent"),
        "litterPct": d.get("litterLevelPercentage"),
        "online": robot.is_online,
    }


async def send_telegram(text: str) -> None:
    """Best-effort Telegram message via the @jaredpi_bot. Never raises."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            )
    except Exception as ex:
        log_event("telegram_error", error=str(ex))


async def heartbeat() -> None:
    """Best-effort ping to the healthchecks.io dead-man's switch. Never raises."""
    if not HEALTHCHECKS_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(HEALTHCHECKS_URL)
    except Exception as ex:
        log_event("heartbeat_error", error=str(ex))


class Watcher:
    def __init__(self, robot):
        self.robot = robot
        self.lock = asyncio.Lock()
        self.last_status = None
        self.incident_resets = 0
        self.capped = False
        # offline-failsafe state
        self.offline_since = None
        self.offline_alerted = False
        self.last_online_status = None

    async def evaluate(self) -> None:
        # Single-flight: if an evaluation/reset is already in progress, skip;
        # the running one reads fresh state anyway.
        if self.lock.locked():
            return
        async with self.lock:
            snap = snapshot(self.robot)
            status = snap["status"]

            if status != self.last_status:
                log_event("status", **snap)
                self.last_status = status

            # --- Offline failsafe (Mode A): alert if the robot is unreachable ---
            now = asyncio.get_event_loop().time()
            if not snap["online"]:
                if self.offline_since is None:
                    self.offline_since = now
                elif not self.offline_alerted and (now - self.offline_since) >= OFFLINE_ALERT_SECONDS:
                    mins = int(OFFLINE_ALERT_SECONDS // 60)
                    mid = " It was mid-cycle when it dropped." if self.last_online_status in MID_CYCLE_STATES else ""
                    log_event("offline_alert", last_online_status=self.last_online_status, **snap)
                    await send_telegram(f"⚠️ Litter-Robot offline for {mins}+ min — can't reach status.{mid}")
                    self.offline_alerted = True
                return
            else:
                if self.offline_alerted:
                    log_event("recovered_alert", **snap)
                    await send_telegram("✅ Litter-Robot back online.")
                self.offline_since = None
                self.offline_alerted = False
                self.last_online_status = status

            if status in STUCK_STATES:
                w = snap.get("weight") or 0
                if w > WEIGHT_THRESHOLD:
                    log_event("hold_weight_present", **snap)  # real cat -> never reset
                elif self.incident_resets >= RESET_CAP:
                    if not self.capped:
                        log_event("reset_cap_reached", resets=self.incident_resets, **snap)
                        self.capped = True
                else:
                    log_event("reset_press", attempt=self.incident_resets + 1, **snap)
                    try:
                        await self.robot.reset()
                        self.incident_resets += 1
                    except Exception as ex:
                        log_event("reset_error", error=str(ex))
            elif status in HEALTHY_STATES:
                if self.incident_resets or self.capped:
                    log_event("incident_cleared", resets=self.incident_resets, **snap)
                self.incident_resets = 0
                self.capped = False

    def on_update(self) -> None:
        # Sync callback from the event system -> schedule async evaluation.
        asyncio.create_task(self.evaluate())


async def run_once() -> None:
    account = Account()
    await account.connect(
        username=os.environ["LITTER_USER"],
        password=os.environ["LITTER_PASS"],
        load_robots=True,
    )
    try:
        robot = account.robots[0]
        watcher = Watcher(robot)
        log_event("startup", serial=robot.serial, backstop=BACKSTOP,
                  weight_threshold=WEIGHT_THRESHOLD, reset_cap=RESET_CAP)
        robot.on(EVENT_UPDATE, watcher.on_update)
        await robot.subscribe()
        await watcher.evaluate()  # act on current state immediately
        await heartbeat()         # initial "I'm alive" ping to the dead-man's switch
        last_hb = asyncio.get_event_loop().time()
        # Backstop loop: periodic refresh emits EVENT_UPDATE -> evaluate(),
        # guaranteeing action even if a websocket push is missed/dropped.
        # Also emits the heartbeat ping so an external monitor catches a dead watcher.
        while True:
            await asyncio.sleep(BACKSTOP)
            await robot.refresh()
            now = asyncio.get_event_loop().time()
            if now - last_hb >= HEARTBEAT_SECONDS:
                await heartbeat()
                last_hb = now
    finally:
        try:
            await account.disconnect()
        except Exception:
            pass


async def main() -> None:
    while True:
        try:
            await run_once()
        except Exception as ex:
            log_event("connection_error", error=str(ex))
            await asyncio.sleep(10)  # backoff, then reconnect


if __name__ == "__main__":
    asyncio.run(main())
