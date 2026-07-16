#!/usr/bin/env python3
"""Litter-Robot 4 auto-resume watcher.

The LR4 intermittently throws a false CAT_SENSOR_INTERRUPTED mid-cycle (weight
sensor ~0, no cat present). This watcher detects it and presses the reset button
(shortResetPress) to resume the cycle.

Detection — adaptive polling (the device does NOT push state, confirmed by test):
poll slowly while idle; the instant a poll sees a cycle running or stuck, ramp to
fast polling so we can't miss a re-interruption; drop back to slow when the cycle
ends. A safety cap stops fast-polling if a "cycle" somehow never completes.

Action — edge-triggered: press reset exactly ONCE on the transition INTO a stuck
state, never again while it stays stuck (the robot takes ~10-15s to respond; re-
pressing a toggle during that lag flips it back). A real cat (weight present) is
never touched. The next interruption is a fresh edge -> its own single press.

Failsafe: Telegram alert if the robot is unreachable; a healthchecks.io heartbeat
so an external monitor catches a dead watcher. Reconnects on any failure.

Logging: one JSON line per event to stdout (fly logs) and a daily file under LOG_DIR.

Config (env, with defaults):
  LITTER_USER / LITTER_PASS  - Whisker account credentials (required)
  SLOW_POLL_SECONDS          - idle baseline poll interval (default 20)
  FAST_POLL_SECONDS          - poll interval while a cycle is active (default 1)
  MAX_FAST_SECONDS           - safety cap: stop fast-polling after this long (default 600)
  WEIGHT_THRESHOLD_LB        - max weight treated as "no cat" (default 3.0)
  LOG_DIR                    - log directory (default ./logs)
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pylitterbot import Account

BASE = Path(__file__).parent
load_dotenv(BASE / ".env-litter")

SLOW_POLL = float(os.environ.get("SLOW_POLL_SECONDS", 20))   # idle baseline
FAST_POLL = float(os.environ.get("FAST_POLL_SECONDS", 1))    # while a cycle is active
MAX_FAST_SECONDS = float(os.environ.get("MAX_FAST_SECONDS", 600))  # cap on continuous fast-polling
WEIGHT_THRESHOLD = float(os.environ.get("WEIGHT_THRESHOLD_LB", 3.0))

# Persistent auto-reset cadence: while stuck (no cat on the scale), press reset every RESET_RETRY_SECONDS
# until it recovers — indefinitely, not a fixed number of tries. The spacing must stay well above the
# robot's ~10-15s response lag so a press is never re-issued mid-landing (that was the old oscillation).
# Notifications never stop the retries.
RESET_RETRY_SECONDS = float(os.environ.get("RESET_RETRY_SECONDS", 45))       # min gap between reset presses
STUCK_ALERT_SECONDS = float(os.environ.get("STUCK_ALERT_SECONDS", 600))      # first Telegram after stuck this long
STUCK_REALERT_SECONDS = float(os.environ.get("STUCK_REALERT_SECONDS", 1800)) # remind this often while still stuck
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Failsafe / alerting
OFFLINE_ALERT_SECONDS = float(os.environ.get("OFFLINE_ALERT_SECONDS", 300))   # robot offline this long -> Telegram
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", 1800))          # ping healthchecks this often
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HEALTHCHECKS_URL = os.environ.get("HEALTHCHECKS_URL", "")

# Publish robot status to Supabase (device_status table) for the status hub to read.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Closet status light (Govee) — driven over the cloud API so the Fly watcher can reach it.
GOVEE_API_KEY = os.environ.get("GOVEE_API_KEY", "")
GOVEE_LIGHT_SKU = os.environ.get("GOVEE_LIGHT_SKU", "")
GOVEE_LIGHT_DEVICE = os.environ.get("GOVEE_LIGHT_DEVICE", "")
DRAWER_FULL_PCT = float(os.environ.get("DRAWER_FULL_PCT", 90))  # waste drawer >= this -> "needs emptying"
LITTER_LOW_PCT = float(os.environ.get("LITTER_LOW_PCT", 75))    # litter level <= this -> "litter low"

# Status -> Govee DIY scene NAME (authored in the Govee app; edit their look there freely).
# Scene IDs are resolved from these names at startup, so editing a scene's appearance keeps
# working; only renaming or deleting+recreating a scene needs a watcher restart to re-resolve.
SCENE_CYCLING = "Cycling"
SCENE_STUCK = "Stuck"
SCENE_FULL = "Drawer full"
SCENE_LITTER_LOW = "Litter low"
SCENE_OFFLINE = "Disconnected"
_DIY_SCENES: dict = {}  # scene name -> id, populated at startup by load_diy_scenes()

# A clean cycle was in progress when it dropped -> worth calling out in the alert.
MID_CYCLE_STATES = {"CLEAN_CYCLE", "CAT_SENSOR_INTERRUPTED", "PAUSED"}

STUCK_STATES = {"CAT_SENSOR_INTERRUPTED", "PAUSED"}
HEALTHY_STATES = {"CLEAN_CYCLE", "READY", "CLEAN_CYCLE_COMPLETE", "CAT_SENSOR_TIMING", "OFF"}
# A cycle is "active" (globe moving or stuck mid-cycle) -> poll fast to stay on top of it.
ACTIVE_STATES = {"CLEAN_CYCLE", "CAT_SENSOR_INTERRUPTED", "PAUSED"}


def latched_cat_detect(status: str, weight: float) -> bool:
    """CAT_DETECTED while the scale reads empty: the entrance sensor is latched, not occupied.
    A cat has weight, so zero weight means nothing is in the globe. The firmware refuses to cycle
    in this state, so it strands indefinitely (observed: 7h33m) — and it is neither a STUCK_STATE
    nor an ACTIVE_STATE, so nothing else here would ever touch it."""
    return status == "CAT_DETECTED" and (weight or 0) <= WEIGHT_THRESHOLD


def needs_reset(status: str, weight: float) -> bool:
    """Fault conditions a single reset press clears."""
    return status in STUCK_STATES or latched_cat_detect(status, weight)


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


def litter_level_pct(snap: dict):
    """Litter level as a 0-100 percent (the device reports it as a 0-1 fraction)."""
    v = snap.get("litterPct")
    if not isinstance(v, (int, float)):
        return None
    return round(v * 100, 1) if v <= 1.5 else round(v, 1)


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


async def publish_status(serial: str, snap: dict) -> None:
    """Upsert the robot's current status to Supabase (device_status) for the hub. Never raises."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    litter_pct = litter_level_pct(snap)
    row = {
        "device": serial, "name": "Litter-Robot 4", "status": snap.get("status"),
        "litter_level_pct": litter_pct, "waste_drawer_pct": snap.get("drawerPct"),
        "online": snap.get("online"), "updated_at": now_iso(),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/device_status?on_conflict=device",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                json=row,
            )
    except Exception as ex:
        log_event("publish_error", error=str(ex))


def _govee_ready() -> bool:
    return bool(GOVEE_API_KEY and GOVEE_LIGHT_SKU and GOVEE_LIGHT_DEVICE)


async def _govee_ctrl(cap_type: str, instance: str, value) -> None:
    payload = {
        "requestId": str(uuid.uuid4()),
        "payload": {"sku": GOVEE_LIGHT_SKU, "device": GOVEE_LIGHT_DEVICE,
                    "capability": {"type": cap_type, "instance": instance, "value": value}},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://openapi.api.govee.com/router/api/v1/device/control",
            headers={"Govee-API-Key": GOVEE_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )


async def load_diy_scenes() -> None:
    """Resolve DIY scene NAME -> id from the device, so we can trigger scenes by name."""
    if not _govee_ready():
        return
    try:
        payload = {"requestId": str(uuid.uuid4()),
                   "payload": {"sku": GOVEE_LIGHT_SKU, "device": GOVEE_LIGHT_DEVICE}}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://openapi.api.govee.com/router/api/v1/device/diy-scenes",
                headers={"Govee-API-Key": GOVEE_API_KEY, "Content-Type": "application/json"},
                json=payload,
            )
        _DIY_SCENES.clear()
        for c in resp.json().get("payload", {}).get("capabilities", []):
            if c.get("instance") == "diyScene":
                for o in c.get("parameters", {}).get("options", []):
                    _DIY_SCENES[o["name"]] = o["value"]
        log_event("diy_scenes_loaded", scenes=_DIY_SCENES)
    except Exception as ex:
        log_event("diy_scenes_error", error=str(ex))


async def set_closet_light(scene: str) -> None:
    """Trigger a DIY scene on the closet light by name (powers on, then runs it). Never raises."""
    if not _govee_ready():
        return
    sid = _DIY_SCENES.get(scene)
    if sid is None:
        log_event("scene_missing", scene=scene, known=list(_DIY_SCENES))
        return
    try:
        await _govee_ctrl("devices.capabilities.on_off", "powerSwitch", 1)
        await _govee_ctrl("devices.capabilities.dynamic_scene", "diyScene", sid)
        log_event("light", scene=scene, id=sid)
    except Exception as ex:
        log_event("light_error", error=str(ex), scene=scene)


async def get_light_state() -> "dict | None":
    """Read the closet light's current state so it can be restored later. None on failure.

    Captures power/brightness/color/white. A running dynamic scene restores to a static
    approximation (whatever color the device reports), not the scene itself.
    """
    if not _govee_ready():
        return None
    try:
        payload = {"requestId": str(uuid.uuid4()),
                   "payload": {"sku": GOVEE_LIGHT_SKU, "device": GOVEE_LIGHT_DEVICE}}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://openapi.api.govee.com/router/api/v1/device/state",
                headers={"Govee-API-Key": GOVEE_API_KEY, "Content-Type": "application/json"},
                json=payload,
            )
        caps = {c["instance"]: c.get("state", {}).get("value")
                for c in resp.json().get("payload", {}).get("capabilities", [])}
        return {"power": caps.get("powerSwitch"), "brightness": caps.get("brightness"),
                "colorRgb": caps.get("colorRgb"), "colorTempK": caps.get("colorTemperatureK")}
    except Exception as ex:
        log_event("light_read_error", error=str(ex))
        return None


async def apply_light_state(state: "dict | None") -> None:
    """Restore a previously-captured closet light state. Never raises."""
    if not _govee_ready() or not state:
        return
    try:
        if not state.get("power"):
            await _govee_ctrl("devices.capabilities.on_off", "powerSwitch", 0)
            log_event("light_restored", state="off")
            return
        await _govee_ctrl("devices.capabilities.on_off", "powerSwitch", 1)
        if state.get("brightness") is not None:
            await _govee_ctrl("devices.capabilities.range", "brightness", state["brightness"])
        if state.get("colorTempK"):       # was in white/temperature mode
            await _govee_ctrl("devices.capabilities.color_setting", "colorTemperatureK", state["colorTempK"])
        elif state.get("colorRgb") is not None:
            await _govee_ctrl("devices.capabilities.color_setting", "colorRgb", state["colorRgb"])
        log_event("light_restored", brightness=state.get("brightness"),
                  colorTempK=state.get("colorTempK"), colorRgb=state.get("colorRgb"))
    except Exception as ex:
        log_event("light_restore_error", error=str(ex))


class Watcher:
    def __init__(self, robot):
        self.robot = robot
        self.lock = asyncio.Lock()
        self.last_status = None
        # persistent-reset state: retry until it recovers, spaced to avoid the old oscillation
        self.stuck_since = None       # monotonic time the current stuck episode began
        self.last_press = None        # monotonic time of our last reset press
        self.press_count = 0          # reset presses issued this episode (logging)
        self.last_stuck_alert = None  # monotonic time of the last "still stuck" Telegram
        self.holding_logged = False   # logged "cat present, holding" once this episode
        self.pending_cycle = False    # latch was cleared; run the cycle it skipped once READY
        self.controlling = False      # are we currently overriding the closet light?
        self.saved_light = None       # user's light state, captured before we took over
        self.last_override = None     # current override color while controlling
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
                await publish_status(self.robot.serial, snap)

            # Closet status light. The watcher only "takes over" when there's something to
            # show; otherwise it hands the light back to whatever state the user left it in.
            if not snap["online"]:
                override = SCENE_OFFLINE
            elif status == "CLEAN_CYCLE":
                override = SCENE_CYCLING
            elif needs_reset(status, snap.get("weight")):
                override = SCENE_STUCK
            elif (snap.get("drawerPct") or 0) >= DRAWER_FULL_PCT:
                override = SCENE_FULL
            elif (litter_level_pct(snap) or 100) <= LITTER_LOW_PCT:
                override = SCENE_LITTER_LOW
            else:
                override = None                                    # nothing to show -> restore user's light
            await self.update_light(override)

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

            # Persistent auto-reset: while the robot is stuck AND the scale reads empty (no cat), press
            # reset every RESET_RETRY_SECONDS until it recovers — indefinitely, not just once. A single
            # press often doesn't land when the fault is physical/borderline (a clump at the pinch points,
            # a DFI hiccup); spaced retries eventually clear those and cost nothing, since we're already
            # polling. The spacing is what prevents the old oscillation: it's comfortably longer than the
            # robot's ~10-15s response lag, so we never re-press mid-landing, and we only ever press on a
            # poll that STILL sees it stuck. A cat on the scale (weight present) is never touched. We retry
            # forever, but still ping Telegram (first after STUCK_ALERT_SECONDS, then every
            # STUCK_REALERT_SECONDS) so a genuinely wedged unit that needs hands-on help isn't missed —
            # the alert never halts the retries.
            w = snap.get("weight") or 0
            is_stuck = needs_reset(status, w)
            if is_stuck:
                if self.stuck_since is None:
                    self.stuck_since = now
                if w > WEIGHT_THRESHOLD:
                    if not self.holding_logged:
                        log_event("hold_weight_present", **snap)   # real cat present -> never reset
                        self.holding_logged = True
                elif self.last_press is None or (now - self.last_press) >= RESET_RETRY_SECONDS:
                    self.last_press = now   # record BEFORE the press so the spacing holds even if reset() throws
                    self.press_count += 1
                    log_event("reset_press", attempt=self.press_count,
                              stuck_for=round(now - self.stuck_since), **snap)
                    # A latch reset lands on READY without cleaning, so the visit that preceded it never
                    # gets its cycle. Queue one; the block below runs it once READY is observed.
                    self.pending_cycle = latched_cat_detect(status, w)
                    try:
                        await self.robot.reset()
                    except Exception as ex:
                        log_event("reset_error", error=str(ex))
                stuck_for = now - self.stuck_since
                if stuck_for >= STUCK_ALERT_SECONDS and (self.last_stuck_alert is None
                        or (now - self.last_stuck_alert) >= STUCK_REALERT_SECONDS):
                    self.last_stuck_alert = now
                    mins = int(stuck_for // 60)
                    log_event("stuck_alert", stuck_minutes=mins, presses=self.press_count, **snap)
                    await send_telegram(
                        f"⚠️ Litter-Robot stuck ({status}) for {mins}+ min — auto-reset has tried "
                        f"{self.press_count}× and is still retrying; may need a hands-on check.")
            else:
                if self.stuck_since is not None and status in HEALTHY_STATES:
                    log_event("incident_cleared", presses=self.press_count, **snap)
                self.stuck_since = None
                self.last_press = None
                self.press_count = 0
                self.last_stuck_alert = None
                self.holding_logged = False

            # Run the queued cycle only once the press has demonstrably landed (status READY, scale
            # still empty). Gating on observed state rather than a sleep: the robot takes ~10-15s to
            # respond, and if weight shows up in the meantime a cat is really in there — stand down.
            if self.pending_cycle and not is_stuck:
                if w > WEIGHT_THRESHOLD:
                    log_event("latch_cycle_cancelled", **snap)
                    self.pending_cycle = False
                elif status == "READY":
                    log_event("latch_cycle_start", **snap)
                    self.pending_cycle = False
                    try:
                        await self.robot.start_cleaning()
                    except Exception as ex:
                        log_event("latch_cycle_error", error=str(ex))

    async def update_light(self, override: "str | None") -> None:
        """Take over the closet light to show `override`, or hand it back to the user.

        On the first override we capture the user's current light state; while overriding
        we just update the color; when there's nothing to show we restore what we captured.
        """
        if override is not None:
            if not self.controlling:
                self.saved_light = await get_light_state()  # remember the user's light
                self.controlling = True
                self.last_override = None
                log_event("light_takeover", saved=self.saved_light)
            if override != self.last_override:
                await set_closet_light(override)
                self.last_override = override
        elif self.controlling:
            await apply_light_state(self.saved_light)  # hand it back
            self.controlling = False
            self.saved_light = None
            self.last_override = None


# A connect()/refresh() that hangs beyond this becomes a TimeoutError, which propagates to main()'s reconnect
# loop — so a stalled Whisker connection self-heals instead of silently freezing the watcher forever (the
# exact failure we hit: one poll, then a hung refresh with no timeout, no exception, and no reconnect).
REFRESH_TIMEOUT = 30

async def run_once() -> None:
    account = Account()
    await asyncio.wait_for(account.connect(
        username=os.environ["LITTER_USER"],
        password=os.environ["LITTER_PASS"],
        load_robots=True,
    ), REFRESH_TIMEOUT)
    try:
        robot = account.robots[0]
        watcher = Watcher(robot)
        log_event("startup", serial=robot.serial, slow_poll=SLOW_POLL,
                  fast_poll=FAST_POLL, weight_threshold=WEIGHT_THRESHOLD)
        await load_diy_scenes()   # resolve scene names -> ids before we may trigger one
        await asyncio.wait_for(robot.refresh(), REFRESH_TIMEOUT)
        await watcher.evaluate()  # act on current state immediately
        await heartbeat()         # initial "I'm alive" ping to the dead-man's switch
        last_hb = asyncio.get_event_loop().time()

        # Adaptive polling: slow while idle; ramp to fast the instant a cycle is
        # active (running or stuck) so we never miss a re-interruption; drop back to
        # slow when it ends. A safety cap stops fast-polling if a "cycle" never clears.
        fast_since = None
        fast_capped = False
        while True:
            now = asyncio.get_event_loop().time()
            active = robot.status.name in ACTIVE_STATES
            if active:
                if fast_since is None:
                    fast_since = now
                    log_event("cycle_active", status=robot.status.name, poll=FAST_POLL)
                if not fast_capped and (now - fast_since) >= MAX_FAST_SECONDS:
                    fast_capped = True
                    mins = int(MAX_FAST_SECONDS // 60)
                    # Just throttle polling back to slow after a long active stretch (an API-load guard).
                    # This no longer "gives up": reset retries + the stuck alert live in evaluate(), and
                    # RESET_RETRY_SECONDS > SLOW_POLL so presses keep landing on the slow cadence.
                    log_event("fast_poll_slowed", status=robot.status.name, minutes=mins)
                interval = SLOW_POLL if fast_capped else FAST_POLL
            else:
                if fast_since is not None:
                    log_event("cycle_done", status=robot.status.name)
                fast_since = None
                fast_capped = False
                interval = SLOW_POLL

            if now - last_hb >= HEARTBEAT_SECONDS:
                await heartbeat()
                await publish_status(robot.serial, snapshot(robot))  # keep device_status fresh
                last_hb = now

            await asyncio.sleep(interval)
            await asyncio.wait_for(robot.refresh(), REFRESH_TIMEOUT)
            await watcher.evaluate()
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
