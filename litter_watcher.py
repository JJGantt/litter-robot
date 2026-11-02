#!/usr/bin/env python3
"""Litter-Robot 4 auto-resume watcher.

The LR4 intermittently throws a false CAT_SENSOR_INTERRUPTED mid-cycle (weight
sensor ~0, no cat present). This watcher detects it and presses the reset button
(shortResetPress) to resume the cycle.

Detection — adaptive polling (the device does NOT push state, confirmed by test):
poll slowly while idle; the instant a poll sees a cycle running or stuck, ramp to
fast polling so we can't miss a re-interruption; drop back to slow when the cycle
ends. A safety cap stops fast-polling if a "cycle" somehow never completes.

Action: reset ONLY a cycle that started but stalled mid-way (CAT_SENSOR_INTERRUPTED /
PAUSED) — never on a mere CAT_DETECTED (there the globe never moved; resetting would just
spin the box on the cat that was sensed). The stall must hold a continuously empty scale for
a confirm dwell before the first press, so a normal (transient) cat visit is never touched —
only a genuine stall persists that long. Then retry on a spaced cadence (past the robot's
~10-15s response lag, so we never re-press mid-landing) until it clears.

Phantom weight: the opposite failure. The scale drifts off zero on an IDLE box, the robot decides a
cat is standing in it, and after 30 minutes it locks out with a blue bar + partial yellow flashing and
stops cycling. We tare it with the same reset press, gated on the reading being static (a cat's is not).

Lockout: that same 30-minute cat-detect, still showing after the weight behind it has fallen back to
zero. The tare never begins (it waits on a non-zero reading) and the stall watcher wants a mid-cycle
status, so the box sits dead with nothing looking at it. We press reset once the robot's own verdict
has held over an empty scale.

Failsafe: phone push (APNs via the status hub) if the robot is unreachable; a healthchecks.io
heartbeat so an external monitor catches a dead watcher. Reconnects on any failure.

Logging: one JSON line per event to stdout (fly logs) and a daily file under LOG_DIR.

Config (env, with defaults):
  LITTER_USER / LITTER_PASS  - Whisker account credentials (required)
  SLOW_POLL_SECONDS          - idle baseline poll interval (default 20)
  FAST_POLL_SECONDS          - poll interval while a cycle is active (default 1)
  MAX_FAST_SECONDS           - safety cap: stop fast-polling after this long (default 600)
  WEIGHT_THRESHOLD_LB        - max weight treated as "no cat" (default 3.0)
  SCALE_STUCK_SECONDS        - non-zero scale on an idle box this long -> auto-tare (default 1800)
  LOCKOUT_CONFIRM_SECONDS    - 30-min cat-detect held over an empty scale this long -> reset (default 120)
  LOG_DIR                    - log directory (default ./logs)
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pylitterbot import Account

import ops_report as ops

BASE = Path(__file__).parent
load_dotenv(BASE / ".env-litter")

SLOW_POLL = float(os.environ.get("SLOW_POLL_SECONDS", 20))   # idle baseline
FAST_POLL = float(os.environ.get("FAST_POLL_SECONDS", 1))    # while a cycle is active
MAX_FAST_SECONDS = float(os.environ.get("MAX_FAST_SECONDS", 600))  # cap on continuous fast-polling
WEIGHT_THRESHOLD = float(os.environ.get("WEIGHT_THRESHOLD_LB", 3.0))

# Auto-reset cadence: a stall (mid-cycle CAT_SENSOR_INTERRUPTED / PAUSED) must first hold for
# STALL_CONFIRM_SECONDS with no cat flagged by the robot (see check_stall for the gates — the scale
# alone cannot see a cat). Once confirmed, retry every
# RESET_RETRY_SECONDS until it recovers — indefinitely, not a fixed number of tries. The spacing stays
# well above the robot's ~10-15s response lag so a press is never re-issued mid-landing (the old
# oscillation). Notifications never stop the retries.
RESET_RETRY_SECONDS = float(os.environ.get("RESET_RETRY_SECONDS", 30))       # min gap between reset presses
STALL_CONFIRM_SECONDS = float(os.environ.get("STALL_CONFIRM_SECONDS", 60))   # stall must hold this long before the FIRST press
STUCK_ALERT_SECONDS = float(os.environ.get("STUCK_ALERT_SECONDS", 600))      # first alert after stuck this long
STUCK_REALERT_SECONDS = float(os.environ.get("STUCK_REALERT_SECONDS", 1800)) # remind this often while still stuck

# ---- Phantom weight (a scale that never returns to zero) -------------------------------------------
# A second, opposite failure to the stall above. The LR4's scale drifts off zero — a nudge, litter
# added, the drawer emptied, a foot caught on the mat — and the robot then believes a cat is standing
# in it forever. Its firmware gives up after 30 minutes and shows a BLUE light bar with PARTIAL YELLOW
# FLASHING, and while that is showing THE BOX WILL NOT CYCLE. Whisker's own fix is a short reset press,
# which re-zeroes the scale — exactly the button we can already press.
# The stall watcher above can never catch this: it fires on a mid-cycle stall with an EMPTY scale, and
# this is an idle box with a NON-empty one, so it was a blind spot by construction.
# Gates before we press, so a real cat is never spun:
#   * the box must be idle (no cycle running, not stuck — those are the reset path's job);
#   * the scale must read non-zero continuously for SCALE_STUCK_SECONDS. The default is the robot's own
#     30 minutes, so we are never more aggressive than the manufacturer's "this cannot be a cat";
#   * the reading must be essentially STATIC across that window. A live animal's weight wanders as it
#     shifts; a drifted zero is dead flat. If it wanders we do not press — we alert instead.
SCALE_ZERO_TOL = float(os.environ.get("SCALE_ZERO_TOL", 0.2))            # |lb| below this = a zeroed scale
SCALE_STUCK_SECONDS = float(os.environ.get("SCALE_STUCK_SECONDS", 1800)) # non-zero this long on an idle box = phantom
SCALE_STATIC_TOL = float(os.environ.get("SCALE_STATIC_TOL", 0.25))       # max peak-to-peak lb across that window
SCALE_RETRY_SECONDS = float(os.environ.get("SCALE_RETRY_SECONDS", 120))  # min gap between tare presses
SCALE_ALERT_AFTER = int(os.environ.get("SCALE_ALERT_AFTER", 3))          # presses that fail to clear it -> call a human

# ---- Firmware lockout latched over a scale that reads zero -----------------------------------------
# The third failure, and the one nothing else can reach. The robot trips its own 30-minute cat-detect
# lockout and stops cycling — but the weight it latched on then falls back to zero. check_scale starts
# its dwell on a NON-ZERO reading, so it sees a clean, idle box and never begins; the stall watcher
# needs a mid-cycle status and this one is READY. The box is simply dead until a human presses reset.
# Observed 2026-08-27: 80 minutes locked out with weight 0.0, no detector even looking at it.
# DC_CAT_DETECT_30M is the lockout specifically. DC_CAT_DETECT (no suffix) is an ordinary cat on the
# scale and must never be acted on — that is a live animal, and the two differ only by that suffix.
LOCKOUT_DISPLAY_CODES = {"DC_CAT_DETECT_30M"}
LOCKOUT_CONFIRM_SECONDS = float(os.environ.get("LOCKOUT_CONFIRM_SECONDS", 120))  # hold across polls before the first press
LOCKOUT_RETRY_SECONDS = float(os.environ.get("LOCKOUT_RETRY_SECONDS", 300))      # min gap between presses
LOCKOUT_ALERT_AFTER = int(os.environ.get("LOCKOUT_ALERT_AFTER", 3))              # presses that fail to clear it -> call a human

# ---- Usage-driven cleaning -----------------------------------------------------------------------
# The robot is kept in SLEEP mode (set once in the Whisker app) so it never auto-cycles; WE fire the
# clean. A visit is the robot's own isCatDetectPending flag going true — its verdict that a cat used
# the box and a cycle is owed, which sleep mode stops it acting on. Wait CLEAN_DWELL_S from the LAST
# visit, then start_cleaning() — but ONLY while online + sleeping + idle, so a lost connection just
# parks it idle (never a stuck offline cycle). The existing stall watcher still rescues any stall from
# the cycle we fire. If the robot ever leaves sleep mode we alert (the sleep schedule can't be set via
# the API — that write reverts — so it's monitored, not auto-repaired).
#
# Why the flag and nothing else (measured 2026-08-29/30, asleep and awake): the cloud copy of the
# live scale (weightSensor) is never published — 10 lb sat on the scale for 8 minutes and every
# read said 0.0 — and the CAT_DETECTED status only appears while awake. The recorded pet weight
# (catWeight) is a side effect of the same detection that the SmartScale scores for roughly half of
# visits; four real visits in 42 hours produced no record at all. It stays as a secondary trigger
# because when it does fire it refreshes the dwell (the flag is a boolean, so a second cat inside the
# window is otherwise invisible). The robot clears the flag when any cycle starts, ours or a button
# press, so the falling edge means "clean" and re-arms the next visit with no bookkeeping.
CLEAN_DWELL_S = float(os.environ.get("CLEAN_DWELL_S", 15 * 60))    # min quiet after the last visit
CLEAN_MIN_GAP_S = float(os.environ.get("CLEAN_MIN_GAP_S", 120))    # never re-fire within this of the last clean
DRIVE_CLEANING = os.environ.get("DRIVE_CLEANING", "1") not in ("0", "false", "off", "no", "")
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Failsafe / alerting
OFFLINE_ALERT_SECONDS = float(os.environ.get("OFFLINE_ALERT_SECONDS", 180))   # robot offline this long -> high-priority alert
# High-priority phone push via the status-hub app (APNs). Same /api/notify the voice worker uses; the
# token is the hub's TV_TOKEN. Every alert (offline, sleep dropped, stuck, drawer, scale) goes here.
HUB_NOTIFY_URL = os.environ.get("HUB_NOTIFY_URL", "")
HUB_NOTIFY_TOKEN = os.environ.get("HUB_NOTIFY_TOKEN", "")
# The dead-man ping is fed once per connection cycle (see RECONNECT_EVERY_S), so it needs no interval
# of its own. device_status does: the hub's litter card is only as fresh as our last write, and writing
# it solely on a status change left the card reading hours stale whenever the robot sat idle.
PUBLISH_SECONDS = float(os.environ.get("PUBLISH_SECONDS", 600))               # refresh device_status at least this often
HEALTHCHECKS_URL = os.environ.get("HEALTHCHECKS_URL", "")

# Publish robot status to Supabase (device_status table) for the status hub to read.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Closet status light (Govee) — driven over the cloud API so the Fly watcher can reach it.
GOVEE_API_KEY = os.environ.get("GOVEE_API_KEY", "")
GOVEE_LIGHT_SKU = os.environ.get("GOVEE_LIGHT_SKU", "")
GOVEE_LIGHT_DEVICE = os.environ.get("GOVEE_LIGHT_DEVICE", "")
DRAWER_FULL_PCT = float(os.environ.get("DRAWER_FULL_PCT", 90))  # waste drawer >= this -> "needs emptying"
DRAWER_REALERT_SECONDS = float(os.environ.get("DRAWER_REALERT_SECONDS", 6 * 3600))  # remind this often while full
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
# Idle: the globe is parked and the scale SHOULD read zero, so anything on it is a cat or a drift.
SCALE_IDLE_STATES = {"READY", "CLEAN_CYCLE_COMPLETE", "DRAWER_FULL"}
HEALTHY_STATES = {"CLEAN_CYCLE", "READY", "CLEAN_CYCLE_COMPLETE", "CAT_SENSOR_TIMING", "OFF"}
# A cycle is "active" (globe moving or stuck mid-cycle) -> poll fast to stay on top of it.
ACTIVE_STATES = {"CLEAN_CYCLE", "CAT_SENSOR_INTERRUPTED", "PAUSED"}
# Also fast-poll while a cat is present, so a visit is never missed (it drives the usage trigger).
POLL_FAST_STATES = ACTIVE_STATES | {"CAT_DETECTED", "CAT_SENSOR_TIMING"}


def needs_reset(status: str) -> bool:
    """A reset is warranted ONLY when a cycle STARTED but didn't FINISH — the globe stalled
    mid-cycle (CAT_SENSOR_INTERRUPTED / PAUSED). We never reset on CAT_DETECTED: the globe hasn't
    moved there, nothing is stuck, and pressing reset would only spin the box on a cat that was
    just sensed. Empty-scale and a persistence dwell are confirmed by the caller before pressing."""
    return status in STUCK_STATES


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iso_at(epoch: "float | None") -> "str | None":
    """Wall-clock ISO for an epoch seconds value. The watcher times everything on the event loop's
    monotonic clock (immune to NTP steps), but the hub needs a real timestamp to count down from."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def log_event(event: str, **fields) -> None:
    rec = {"ts": now_iso(), "event": event, **fields}
    print(json.dumps(rec), flush=True)
    try:
        with (LOG_DIR / f"litter-{datetime.now().strftime('%Y-%m-%d')}.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # ephemeral fs (e.g. no volume mounted) -> stdout still has it
    _persist_event(rec)  # durable, best-effort mirror to Supabase litter_events
    # Every failure here is an `*_error` event, so this one line is what puts them in front of the ops
    # responder (health/responder), where a new incident starts an agent.
    if event.endswith("_error"):
        ops.report(f"{event}: {fields.get('error', '')}", context=fields)


# The on-disk log above is wiped on every Fly redeploy, so meaningful events are also mirrored to
# Supabase (litter_events). These are too noisy / not about the robot to keep: closet-light control and
# the 10-min reconnect churn (startup / scene reload / reconnect_cycle). Everything else persists.
SKIP_PERSIST = {"light", "light_takeover", "light_restored", "diy_scenes_loaded", "diy_scenes_stale",
                "reconnect_cycle", "startup"}
_CORE_KEYS = {"ts", "event", "status", "cycleState", "cycleStatus", "weight", "catWeight",
              "catDetect", "drawerPct", "litterPct", "is_sleeping", "online"}
_serial = None  # robot serial, set once connected; stamped onto persisted events


def _persist_event(rec: dict) -> None:
    """Fire-and-forget a durable copy of a meaningful event to Supabase litter_events. Never raises.
    Runs on the main event loop, so it survives run_once() reconnects (only a process exit drops it)."""
    if not (SUPABASE_URL and SUPABASE_KEY) or rec.get("event") in SKIP_PERSIST:
        return
    try:
        asyncio.get_running_loop().create_task(_persist_event_async(rec))
    except RuntimeError:
        pass  # no running loop -> the stdout/file copies still have the event


async def _persist_event_async(rec: dict) -> None:
    lp = rec.get("litterPct")
    litter_pct = round(lp * 100, 1) if isinstance(lp, (int, float)) and lp <= 1.5 else lp
    row = {
        "ts": rec.get("ts"), "device": _serial, "event": rec.get("event"),
        "status": rec.get("status"), "cycle_state": rec.get("cycleState"),
        "cycle_status": rec.get("cycleStatus"), "weight": rec.get("weight"),
        "cat_weight": rec.get("catWeight"), "cat_detect": rec.get("catDetect"),
        "drawer_pct": rec.get("drawerPct"), "litter_pct": litter_pct,
        "is_sleeping": rec.get("is_sleeping"), "online": rec.get("online"),
        "data": {k: v for k, v in rec.items() if k not in _CORE_KEYS} or None,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/litter_events",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=row,
            )
            if r.status_code >= 300:   # raw print, NOT log_event, so a failure can't recurse
                print(json.dumps({"ts": now_iso(), "event": "litter_events_error",
                                  "code": r.status_code, "detail": r.text[:160]}), flush=True)
                ops.report(f"litter_events_error: {r.status_code} {r.text[:160]}")
    except Exception as ex:   # the mirror is best-effort and never disrupts the watcher, but it is reported
        ops.report(f"litter_events_error: {ex}")


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
        "is_sleeping": bool(getattr(robot, "is_sleeping", False)),
        "catWeight": d.get("catWeight"),
        "catDetectPending": d.get("isCatDetectPending"),
        # The raw firmware display code — what the light bar is actually showing. pylitterbot only
        # maps one value of it into .status, so anything else (the phantom-weight trip included) is
        # invisible in `status` alone. Carried so the LED state lands in the durable log.
        "displayCode": d.get("displayCode"),
    }


def litter_level_pct(snap: dict):
    """Litter level as a 0-100 percent (the device reports it as a 0-1 fraction)."""
    v = snap.get("litterPct")
    if not isinstance(v, (int, float)):
        return None
    return round(v * 100, 1) if v <= 1.5 else round(v, 1)


async def push_app(title: str, body: str, kind: str = "litter") -> None:
    """Best-effort high-priority phone push through the status-hub app (APNs). Never raises."""
    if not (HUB_NOTIFY_URL and HUB_NOTIFY_TOKEN):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(HUB_NOTIFY_URL, headers={"X-Hub-Token": HUB_NOTIFY_TOKEN},
                                  json={"kind": kind, "title": title, "body": body})
            if r.status_code != 200:
                log_event("app_push_error", status=r.status_code, detail=r.text[:120])
    except Exception as ex:
        log_event("app_push_error", error=str(ex))


async def notify(title: str, body: str, kind: str = "litter") -> None:
    """High-priority alert -> the phone (APNs push through the hub). Never raises."""
    await push_app(title, body, kind)


async def heartbeat() -> None:
    """Best-effort ping to the healthchecks.io dead-man's switch. Never raises."""
    if not HEALTHCHECKS_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(HEALTHCHECKS_URL)
    except Exception as ex:
        log_event("heartbeat_error", error=str(ex))


async def publish_row(row: dict) -> None:
    """Upsert one device_status row to Supabase for the hub to read. Never raises."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/device_status?on_conflict=device",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                json={**row, "updated_at": now_iso()},
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
        resp.raise_for_status()
        scenes = {}
        for c in resp.json().get("payload", {}).get("capabilities", []):
            if c.get("instance") == "diyScene":
                for o in c.get("parameters", {}).get("options", []):
                    scenes[o["name"]] = o["value"]
        if not scenes:
            raise ValueError(f"no DIY scenes in response: {resp.text[:200]}")
    except Exception as ex:
        # repr, not str: an httpx timeout stringifies to "".
        if _DIY_SCENES:
            # This runs on every 10-min reconnect and the last good mapping is kept, so one slow
            # Govee call changes nothing. A light that really cannot be driven reports light_error.
            log_event("diy_scenes_stale", error=repr(ex))
        else:
            log_event("diy_scenes_error", error=repr(ex))
        return
    _DIY_SCENES.clear()
    _DIY_SCENES.update(scenes)
    log_event("diy_scenes_loaded", scenes=_DIY_SCENES)


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
        self.last_stuck_alert = None  # monotonic time of the last "still stuck" alert
        self.holding_logged = False   # logged "cat present, holding" once this episode
        self.cat_in_stall = False     # the robot flagged a cat during this stall: never press it
        self.controlling = False      # are we currently overriding the closet light?
        self.saved_light = None       # user's light state, captured before we took over
        self.last_override = None     # current override color while controlling
        # offline-failsafe state
        self.offline_since = None
        self.offline_alerted = False
        self.last_online_status = None
        # usage-driven clean-trigger state
        self.dirty = False            # a cat has used the box since the last cycle
        self.last_present = None      # monotonic time a visit was last seen (the dwell counts from here)
        self.last_visit_at = None     # same instant in epoch seconds, for the hub's countdown
        self.last_pending = None      # last isCatDetectPending read (None = no read yet this process)
        self.last_cat_weight = None   # last recorded pet weight (a change = a visit the SmartScale scored)
        self.last_clean_fired = None  # monotonic time we last issued start_cleaning()
        self.last_clean_at = None     # epoch seconds a cycle last began (ours or a button press)
        self.not_sleeping_alerted = False  # alerted that the robot dropped out of sleep mode
        self.drawer_full_since = None      # monotonic time the waste drawer first read full
        self.last_drawer_alert = None      # monotonic time of the last drawer-full alert
        # phantom-weight state: a scale that never came back to zero on an idle box
        self.scale_since = None            # monotonic time the scale first read non-zero this episode
        self.scale_min = None              # min/max seen since -> how much the reading wanders
        self.scale_max = None
        self.scale_presses = 0             # tare presses issued this episode
        self.last_scale_press = None       # monotonic time of the last tare press
        self.scale_alerted = False         # already asked for a human this episode
        self.scale_flagged = False         # confirmed phantom right now (drives the closet light)
        self.last_display_code = None      # last firmware display code seen (logged on change)
        # lockout state: the robot's own 30-minute cat-detect, latched over a scale that reads zero
        self.lockout_since = None          # monotonic time the lockout was first seen this episode
        self.lockout_presses = 0           # reset presses issued this episode
        self.last_lockout_press = None     # monotonic time of the last press
        self.lockout_alerted = False       # already asked for a human this episode
        self.lockout_flagged = False       # locked out right now (drives the closet light)
        self.last_publish = None           # monotonic time device_status was last written
        self.published = None              # the row body last written (a change re-publishes at once)

    async def evaluate(self) -> None:
        # Single-flight: if an evaluation/reset is already in progress, skip;
        # the running one reads fresh state anyway.
        if self.lock.locked():
            return
        async with self.lock:
            snap = snapshot(self.robot)
            status = snap["status"]

            now = asyncio.get_event_loop().time()

            if status != self.last_status:
                log_event("status", **snap)
                self.last_status = status

            # The display code is the light bar. Log every change: it is the only record of what the
            # robot was showing, and most of its values never reach `status` at all.
            if snap.get("displayCode") != self.last_display_code:
                log_event("display_code", **snap)
                self.last_display_code = snap.get("displayCode")

            # Closet status light. The watcher only "takes over" when there's something to
            # show; otherwise it hands the light back to whatever state the user left it in.
            if not snap["online"]:
                override = SCENE_OFFLINE
            elif status == "CLEAN_CYCLE":
                override = SCENE_CYCLING
            elif needs_reset(status) or self.scale_flagged or self.lockout_flagged:
                override = SCENE_STUCK
            elif (snap.get("drawerPct") or 0) >= DRAWER_FULL_PCT:
                override = SCENE_FULL
            elif (litter_level_pct(snap) or 100) <= LITTER_LOW_PCT:
                override = SCENE_LITTER_LOW
            else:
                override = None                                    # nothing to show -> restore user's light
            await self.update_light(override)

            # --- Offline failsafe (Mode A): alert if the robot is unreachable ---
            if not snap["online"]:
                # A stale reading proves nothing; start both episodes over when it returns. The lockout
                # dwell especially: carried across an outage it would already be satisfied on
                # reconnect, and press on the very first snapshot back — the stale-data case the dwell
                # exists to catch.
                self._end_scale_episode()
                self._end_lockout_episode()
                if self.offline_since is None:
                    self.offline_since = now
                elif not self.offline_alerted and (now - self.offline_since) >= OFFLINE_ALERT_SECONDS:
                    mins = int(OFFLINE_ALERT_SECONDS // 60)
                    mid = " It was mid-cycle when it dropped." if self.last_online_status in MID_CYCLE_STATES else ""
                    log_event("offline_alert", last_online_status=self.last_online_status, **snap)
                    await notify("🚨 Litter-Robot offline",
                                 f"Unreachable for {mins}+ min — it likely dropped WiFi, which can also knock it "
                                 f"out of sleep mode (it would start auto-cycling on its own).{mid} "
                                 f"Check the robot / router.",
                                 "litter_offline")
                    self.offline_alerted = True
                await self.publish_state(snap, now)
                return
            else:
                if self.offline_alerted:
                    log_event("recovered_alert", **snap)
                    slept = (" Still in sleep mode — good." if snap.get("is_sleeping")
                             else " ⚠️ It came back NOT in sleep mode — re-enable Sleep in the Whisker app.")
                    await notify("✅ Litter-Robot back online", f"Reconnected.{slept}", "litter_online")
                self.offline_since = None
                self.offline_alerted = False
                self.last_online_status = status

            await self.check_stall(snap, now)

            # ---- Phantom weight: an idle box whose scale never came back to zero. Left alone it
            # trips the robot's own 30-minute lockout (blue bar, partial yellow flashing) and the box
            # stops cycling entirely until someone presses reset. ----
            await self.check_scale(snap, now)

            # ---- Lockout: the robot latched its own 30-minute cat-detect and the weight that caused
            # it has since fallen back to zero, so check_scale above never starts a dwell and the box
            # stays dead. Runs after it, and declines any episode that one owns. ----
            await self.check_lockout(snap, now)

            # ---- Usage-driven cleaning: we drive the cycle; the robot stays in sleep so it never
            # auto-cycles. Runs only when online (offline returns above), so a drop just parks it idle. ----
            if DRIVE_CLEANING:
                await self.maybe_clean(snap, now)

            # Last: the driver state published below is read from the flags every check above sets,
            # so publishing earlier would ship the previous poll's verdict.
            await self.publish_state(snap, now)

    def driver_state(self, snap: dict) -> dict:
        """What the WATCHER is doing — none of which the robot's own status reports. Held in sleep
        mode the box reads READY through a queued clean, a phantom weight and a 30-minute lockout
        alike, so a hub painting `status` alone shows an idle robot in every one of them."""
        sleeping = bool(snap.get("is_sleeping"))
        drawer = snap.get("drawerPct") or 0
        return {
            # driving is the whole model: sleep mode is what stops the robot cycling on its own, so
            # out of it the watcher is a bystander and the box is running itself.
            "driving": bool(DRIVE_CLEANING and sleeping),
            "sleeping": sleeping,
            "clean_pending": bool(self.dirty),
            "clean_due_at": (iso_at(self.last_visit_at + CLEAN_DWELL_S)
                             if self.dirty and self.last_visit_at is not None else None),
            "last_visit_at": iso_at(self.last_visit_at),
            "last_clean_at": iso_at(self.last_clean_at),
            # A pending clean survives a full drawer — it fires the moment the drawer is emptied —
            # so the block is carried beside it rather than clearing it.
            "blocked": "drawer_full" if drawer >= DRAWER_FULL_PCT else None,
            "locked_out": bool(self.lockout_flagged),
            "scale_stuck": bool(self.scale_flagged),
            "resetting": bool(self.press_count),
            "display_code": snap.get("displayCode"),
        }

    async def seed_history(self) -> None:
        """Recover last-visit / last-clean from the durable log at startup. Episode state is memory
        only, so every redeploy otherwise blanks the hub's "used" time until the next cat — the one
        thing that card is asked for. Display only: the dwell runs off its own monotonic clock, and
        whether a clean is owed comes from the robot's pending flag, never from these."""
        if not (SUPABASE_URL and SUPABASE_KEY):
            return
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        # A cycle START is a `status` event reading CLEAN_CYCLE — that catches a button press as well
        # as our own drive_clean, which is the same thing the in-memory stamp records.
        wanted = (("last_visit_at", "event=eq.visit"),
                  ("last_clean_at", "event=eq.status&status=eq.CLEAN_CYCLE"))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for field, query in wanted:
                    if getattr(self, field) is not None:
                        continue   # a live event already beat us to it
                    r = await client.get(
                        f"{SUPABASE_URL}/rest/v1/litter_events?select=ts&{query}"
                        f"&device=eq.{self.robot.serial}&order=ts.desc&limit=1", headers=headers)
                    rows = r.json() if r.status_code < 300 else []
                    if rows:
                        setattr(self, field, datetime.fromisoformat(rows[0]["ts"]).timestamp())
        except Exception as ex:
            log_event("seed_error", error=str(ex))

    async def publish_state(self, snap: dict, now: float) -> None:
        """Mirror robot + driver state to device_status: on any change, and at least every
        PUBLISH_SECONDS. Change-driven because a driven clean's whole life — visit, dwell, cycle —
        fits inside one refresh interval, and a card that only refreshes on a timer misses it."""
        row = {
            "device": self.robot.serial, "name": "Litter-Robot 4", "status": snap.get("status"),
            "litter_level_pct": litter_level_pct(snap), "waste_drawer_pct": snap.get("drawerPct"),
            "online": snap.get("online"), "data": self.driver_state(snap),
        }
        if (row == self.published and self.last_publish is not None
                and (now - self.last_publish) < PUBLISH_SECONDS):
            return
        await publish_row(row)
        self.published = row
        self.last_publish = now

    async def check_stall(self, snap: dict, now: float) -> None:
        """Auto-reset ONLY a cycle that started but stalled mid-way (CAT_SENSOR_INTERRUPTED / PAUSED).
        Three gates before we ever press:
          1. No cat flagged by the robot — isCatDetectPending going true at any point in the stall.
             The robot clears that flag when a cycle starts, so true mid-cycle means it registered a
             cat arriving, and the episode is latched hands-off: the robot resumes on its own once
             the cat has gone, and if it never does a human is alerted, never a press. This is the
             only live cat signal the cloud carries while asleep (see CLAUDE.md); the weight gate
             below cannot see an animal. A cat that walks in on a cycle is this case, and a 30s
             press without this gate restarted the globe on one (2026-10-23 13:40Z).
          2. Empty scale — kept, but it only ever sees a stuck reading, never a cat.
          3. Persistence — the stall must hold for STALL_CONFIRM_SECONDS before the FIRST press.
             The robot raises the pending flag some seconds after it halts (16s on 2026-10-23), so
             this dwell is also what gives gate 1 time to see it.
        Once confirmed, retry every RESET_RETRY_SECONDS (spaced past the robot's ~10-15s response
        lag so we never re-press mid-landing) until it clears — indefinitely. The stuck alert fires
        after STUCK_ALERT_SECONDS, then every STUCK_REALERT_SECONDS, without ever halting the retries."""
        status = snap["status"]
        w = snap.get("weight") or 0
        if not needs_reset(status):
            if self.stuck_since is not None and status in HEALTHY_STATES:
                log_event("incident_cleared", presses=self.press_count, cat=self.cat_in_stall, **snap)
            self.stuck_since = None
            self.last_press = None
            self.press_count = 0
            self.last_stuck_alert = None
            self.holding_logged = False
            self.cat_in_stall = False
            return

        if snap.get("catDetectPending") and not self.cat_in_stall:
            self.cat_in_stall = True
            log_event("hold_cat_pending", **snap)

        if w > WEIGHT_THRESHOLD:
            # A cat is on the scale mid-stall -> real cat, never reset. Restart the confirm
            # clock so that once it leaves we re-confirm an empty scale from scratch.
            self.stuck_since = None
            self.last_press = None
            if not self.holding_logged:
                log_event("hold_weight_present", **snap)
                self.holding_logged = True
            return

        if self.stuck_since is None:
            self.stuck_since = now
        held = now - self.stuck_since
        if not self.cat_in_stall and held >= STALL_CONFIRM_SECONDS and (
                self.last_press is None or (now - self.last_press) >= RESET_RETRY_SECONDS):
            self.last_press = now   # record BEFORE the press so spacing holds even if reset() throws
            self.press_count += 1
            log_event("reset_press", attempt=self.press_count, stuck_for=round(held), **snap)
            try:
                await self.robot.reset()
            except Exception as ex:
                log_event("reset_error", error=str(ex))
        if held >= STUCK_ALERT_SECONDS and (self.last_stuck_alert is None
                or (now - self.last_stuck_alert) >= STUCK_REALERT_SECONDS):
            self.last_stuck_alert = now
            mins = int(held // 60)
            log_event("stuck_alert", stuck_minutes=mins, presses=self.press_count, cat=self.cat_in_stall, **snap)
            if self.cat_in_stall:
                # TODO(copy): Jared to word this one; values only until then.
                body = f"{status} · {mins} min · cat detected · no auto-reset"
            else:
                body = (f"{status} for {mins}+ min — auto-reset has tried {self.press_count}× "
                        f"and is still retrying; may need a hands-on check.")
            await notify("⚠️ Litter-Robot stuck", body, "litter_stuck")

    def _end_scale_episode(self) -> None:
        self.scale_since = None
        self.scale_min = self.scale_max = None
        self.scale_presses = 0
        self.last_scale_press = None
        self.scale_alerted = False
        self.scale_flagged = False

    async def check_scale(self, snap: dict, now: float) -> None:
        """Tare a scale that has drifted off zero, before (or after) the robot locks itself out.

        Only ever runs on an IDLE box: a cycle in flight, a stall, a cat-detect status and an offline
        robot all belong to other paths, and a moving globe makes the reading meaningless anyway.
        Presses only once the reading has been non-zero AND essentially static for SCALE_STUCK_SECONDS
        — a cat shifts its weight, a drifted zero does not — so a real animal is never spun.
        """
        status = snap["status"]
        w = snap.get("weight")
        if status not in SCALE_IDLE_STATES or not isinstance(w, (int, float)):
            self._end_scale_episode()
            return

        if abs(w) <= SCALE_ZERO_TOL:
            if self.scale_since is not None:
                log_event("scale_cleared", held_min=round((now - self.scale_since) / 60),
                          presses=self.scale_presses, **snap)
            self._end_scale_episode()
            return

        if self.scale_since is None:
            self.scale_since = now
            self.scale_min = self.scale_max = w
            log_event("scale_nonzero", **snap)
        self.scale_min = min(self.scale_min, w)
        self.scale_max = max(self.scale_max, w)
        held = now - self.scale_since
        if held < SCALE_STUCK_SECONDS:
            return

        spread = round(self.scale_max - self.scale_min, 2)
        self.scale_flagged = True
        if spread > SCALE_STATIC_TOL:
            # It wanders -> something really is in there (a cat that has settled in for the afternoon,
            # or litter sliding around). Never press; just say so once, because the robot has locked
            # itself out by now and will not cycle until it clears.
            if not self.scale_alerted:
                self.scale_alerted = True
                log_event("scale_occupied", held_min=round(held / 60), spread=spread, **snap)
                await notify("⚠️ Litter-Robot scale still loaded",
                             f"{round(w, 1)} lb on the scale for {int(held // 60)}+ min and still moving, "
                             f"so it is not being auto-tared. The robot will not cycle until it clears.",
                             "litter_scale")
            return

        if self.last_scale_press is not None and (now - self.last_scale_press) < SCALE_RETRY_SECONDS:
            return
        self.last_scale_press = now
        self.scale_presses += 1
        log_event("scale_tare", attempt=self.scale_presses, held_min=round(held / 60),
                  spread=spread, **snap)
        try:
            await self.robot.reset()   # short reset press = zero the weight scale
        except Exception as ex:
            log_event("scale_tare_error", error=str(ex))

        if self.scale_presses >= SCALE_ALERT_AFTER and not self.scale_alerted:
            # Reset is the manufacturer's own fix; if it will not take, the base is the suspect
            # (Whisker replaces it under WhiskerCare for exactly this symptom).
            self.scale_alerted = True
            await notify("🚨 Litter-Robot scale stuck",
                         f"The scale has read {round(w, 1)} lb on an empty, idle box for "
                         f"{int(held // 60)}+ min and {self.scale_presses} reset presses have not zeroed "
                         f"it. Check the feet/mat and the litter level; if it keeps happening the base "
                         f"scale is failing.",
                         "litter_scale")

    def _end_lockout_episode(self) -> None:
        self.lockout_since = None
        self.lockout_presses = 0
        self.last_lockout_press = None
        self.lockout_alerted = False
        self.lockout_flagged = False

    async def check_lockout(self, snap: dict, now: float) -> None:
        """Clear a 30-minute cat-detect lockout that is latched over an EMPTY scale.

        While it shows, the box will not cycle at all, and no other path here can end it: check_scale
        begins its dwell on a non-zero reading and this reading has already fallen back to zero, and the
        stall watcher wants a mid-cycle status where this one is READY. So the box stays dead until a
        human presses the button — 80 minutes of it on 2026-08-27, with every detector looking elsewhere.

        This does not weaken the auto-tare's gates; it presses on a strictly narrower case. Required:
        an idle box (a parked globe — the same state check_scale tares in, where a reset zeroes the
        scale rather than moving anything), the robot's OWN verdict that 30 minutes of continuous
        detection cannot be an animal, and a scale reading zero, so there is nothing on it to spin. A
        non-zero reading is check_scale's episode and this one declines it — which is what keeps a
        settled cat, whose weight wanders, on the path that alerts instead of pressing.
        """
        w = snap.get("weight")
        locked = (snap.get("displayCode") in LOCKOUT_DISPLAY_CODES
                  and snap["status"] in SCALE_IDLE_STATES
                  and isinstance(w, (int, float)) and abs(w) <= SCALE_ZERO_TOL)
        if not locked:
            if self.lockout_since is not None:
                log_event("lockout_cleared", held_min=round((now - self.lockout_since) / 60),
                          presses=self.lockout_presses, **snap)
            self._end_lockout_episode()
            return

        if self.lockout_since is None:
            self.lockout_since = now
            log_event("lockout_seen", **snap)
        held = now - self.lockout_since
        # Hold it across several polls first. The Whisker API serves stale snapshots without error, and
        # a code the robot is already leaving is not worth a press.
        if held < LOCKOUT_CONFIRM_SECONDS:
            return
        self.lockout_flagged = True        # confirmed dead until this clears -> closet light says stuck

        if self.last_lockout_press is not None and (now - self.last_lockout_press) < LOCKOUT_RETRY_SECONDS:
            return
        self.last_lockout_press = now
        self.lockout_presses += 1
        log_event("lockout_reset", attempt=self.lockout_presses, held_min=round(held / 60), **snap)
        try:
            await self.robot.reset()   # short reset press = clear the latch and re-zero the scale
        except Exception as ex:
            log_event("lockout_reset_error", error=str(ex))

        if self.lockout_presses >= LOCKOUT_ALERT_AFTER and not self.lockout_alerted:
            self.lockout_alerted = True
            await notify("🚨 Litter-Robot stuck in lockout",
                         f"The box has shown its 30-minute cat-detect lockout for {int(held // 60)}+ min "
                         f"over an empty scale, and {self.lockout_presses} reset presses have not cleared "
                         f"it. It is not cycling until someone touches it.",
                         "litter_lockout")

    async def maybe_clean(self, snap: dict, now: float) -> None:
        """Fire a clean while the robot is held in sleep mode (no auto-cycle). The robot's own
        cat-detect-pending flag marks the box dirty; CLEAN_DWELL_S after the last visit, while
        online + sleeping + idle, WE start the cycle (the stall watcher then rescues any stall)."""
        # Track the flag in every mode: awake the robot clears it itself, and a visit that lands while
        # awake is the robot's to clean; what matters is that the edge is known once sleep resumes.
        pending = snap.get("catDetectPending")
        if pending is not None:
            if pending and not self.last_pending:
                # None -> True: this process's first read finds a visit already owed (a redeploy or a
                # crash mid-dwell). Its age is unknown, so the dwell runs from now.
                self.dirty = True
                self.last_present = now
                self.last_visit_at = time.time()
                log_event("visit", source="pending_at_start" if self.last_pending is None else "pending", **snap)
            elif self.last_pending and not pending:
                self.dirty = False   # a cycle started (ours or a button press): nothing is owed now
                self.last_clean_at = time.time()   # the robot's own confirmation that one began
            self.last_pending = pending

        if not snap.get("is_sleeping"):
            # Suppression dropped — the robot will auto-cycle on its own. Alert once (can't set via API).
            if not self.not_sleeping_alerted:
                log_event("not_sleeping", **snap)
                await notify("🚨 Litter-Robot left sleep mode",
                             "It will auto-cycle on its own now (we are no longer driving it). Re-enable "
                             "Sleep in the Whisker app to hand control back to the watcher.",
                             "litter_sleep")
                self.not_sleeping_alerted = True
            return
        self.not_sleeping_alerted = False

        # Drawer full: NEVER cycle into it (packs the waste down / risks a jam) and tell Jared to empty it.
        # Re-remind while it stays full; clear (and resume) once emptied. `dirty` is preserved below, so the
        # pending clean fires automatically the moment the drawer is emptied.
        drawer = snap.get("drawerPct") or 0
        drawer_full = drawer >= DRAWER_FULL_PCT
        if drawer_full:
            if self.drawer_full_since is None:
                self.drawer_full_since = now
            if self.last_drawer_alert is None or (now - self.last_drawer_alert) >= DRAWER_REALERT_SECONDS:
                log_event("drawer_full", **snap)
                await notify("🚨 Litter-Robot drawer full",
                             f"Waste drawer at {int(drawer)}% — empty it. Cleaning is paused until you do "
                             f"(cycling now would pack it down / risk a jam).",
                             "litter_drawer")
                self.last_drawer_alert = now
        elif self.drawer_full_since is not None and drawer <= DRAWER_FULL_PCT - 10:
            log_event("drawer_emptied", **snap)
            await notify("✅ Litter-Robot drawer emptied",
                         "Waste drawer cleared — driven cleaning resumed.", "litter_drawer")
            self.drawer_full_since = None
            self.last_drawer_alert = None

        cw = snap.get("catWeight")
        status = snap["status"]
        new_weight = (self.last_cat_weight is not None and cw is not None and cw != self.last_cat_weight)
        if cw is not None:
            self.last_cat_weight = cw
        if new_weight:
            # The SmartScale scored a visit. Secondary: it restarts the dwell (a second cat inside the
            # window), and it marks dirty on its own only while the robot also holds a visit pending.
            self.last_present = now
            self.last_visit_at = time.time()
            if pending is not False:
                self.dirty = True
            log_event("visit", source="weight_record", **snap)
            return  # a cat was just there — never cycle inside the dwell

        if drawer_full:
            return  # drawer full: a clean is pending (dirty stays set) but we won't fire until it's emptied
        if pending is False:
            return  # the robot says nothing is owed (it just cycled, or someone pressed the button)

        # DRAWER_FULL is fire-able on purpose: the robot's drawer-full FLAG trips early (seen at 53%
        # depth) yet the box still cycles fine — the real overpack limit is the depth guard above. Only
        # mid-cycle / stuck states are excluded (those are the reset path's job).
        if status not in ("READY", "CLEAN_CYCLE_COMPLETE", "DRAWER_FULL"):
            return
        if not self.dirty or self.last_present is None:
            return
        if (now - self.last_present) < CLEAN_DWELL_S:
            return
        if self.last_clean_fired is not None and (now - self.last_clean_fired) < CLEAN_MIN_GAP_S:
            return
        self.last_clean_fired = now
        log_event("drive_clean", dwell_min=round((now - self.last_present) / 60), **snap)
        try:
            await self.robot.start_cleaning()
            self.dirty = False
        except Exception as ex:
            log_event("drive_clean_error", error=str(ex))

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
# Recycle the whole Whisker connection this often. A HALF-OPEN socket makes refresh() return STALE
# data with no exception and no timeout, so the "hung refresh -> reconnect" guard above never fires and
# the watcher silently goes blind — it stops seeing visits/cycles while still looking alive (the exact
# failure on 2026-08-13: no logs, no drive_clean, for a day). A periodic full reconnect bounds that.
RECONNECT_EVERY_S = float(os.environ.get("RECONNECT_EVERY_S", 600))

async def run_once(watcher: "Watcher") -> None:
    account = Account()
    await asyncio.wait_for(account.connect(
        username=os.environ["LITTER_USER"],
        password=os.environ["LITTER_PASS"],
        load_robots=True,
    ), REFRESH_TIMEOUT)
    try:
        robot = account.robots[0]
        watcher.robot = robot   # re-bind to the fresh robot; the watcher's trigger state persists
        global _serial
        _serial = robot.serial  # stamp persisted events with the device serial
        log_event("startup", serial=robot.serial, slow_poll=SLOW_POLL,
                  fast_poll=FAST_POLL, weight_threshold=WEIGHT_THRESHOLD)
        await load_diy_scenes()   # resolve scene names -> ids before we may trigger one
        await watcher.seed_history()   # before the first publish, so the card is never blank
        await asyncio.wait_for(robot.refresh(), REFRESH_TIMEOUT)
        await watcher.evaluate()  # act on current state immediately
        # Feed the dead-man's switch once per connection. That is the whole cadence: this function
        # returns and is re-entered every RECONNECT_EVERY_S, so the ping lands on that interval. A
        # separate interval here was the bug — its clock was a local, reset on every reconnect, so it
        # never matured and the device_status refresh that hung off it never ran at all.
        await heartbeat()
        conn_start = asyncio.get_event_loop().time()   # when THIS connection opened

        # Adaptive polling: slow while idle; ramp to fast the instant a cycle is
        # active (running or stuck) so we never miss a re-interruption; drop back to
        # slow when it ends. A safety cap stops fast-polling if a "cycle" never clears.
        fast_since = None
        fast_capped = False
        while True:
            now = asyncio.get_event_loop().time()
            active = robot.status.name in POLL_FAST_STATES
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

            # Recycle the connection on a fixed cadence to defeat a silent half-open socket (a stale
            # refresh() that returns with no timeout). Only when idle, so an active cycle is never
            # interrupted; the trigger state lives on the persistent watcher, so a pending clean
            # survives, and the first evaluate() after reconnecting reconciles a changed catWeight.
            if not active and (now - conn_start) >= RECONNECT_EVERY_S:
                log_event("reconnect_cycle", up_min=round((now - conn_start) / 60))
                return

            await asyncio.sleep(interval)
            await asyncio.wait_for(robot.refresh(), REFRESH_TIMEOUT)
            await watcher.evaluate()
            # From the loop that does the work, so a refresh that hangs stops the beat (health/responder).
            ops.beat(60)
    finally:
        try:
            await account.disconnect()
        except Exception as ex:
            log_event("disconnect_error", error=str(ex))


async def main() -> None:
    ops.install(machine="fly", service="litter-robot-watcher")
    watcher = Watcher(None)   # created once so its trigger state (dirty/last_present/…) survives reconnects
    while True:
        try:
            await run_once(watcher)
        except Exception as ex:
            log_event("connection_error", error=str(ex))
            await asyncio.sleep(10)  # backoff, then reconnect


if __name__ == "__main__":
    asyncio.run(main())
