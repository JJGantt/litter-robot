"""Error reporting and heartbeats for a Python service. COPIED into each service from
health/responder/report/report.py -- edit it there and copy it out again (health/responder/README.md).

    import report as ops
    ops.install(machine="pi", service="voice-worker")  # uncaught errors + logging at ERROR and above
    ops.report(exc_or_message, context={...})           # an error caught and handled
    ops.beat(60)                                         # from the loop that does the work

Env: OPS_TOKEN (required to send), OPS_URLS (comma list, first that answers wins; default the Fly
responder), OPS_SPOOL (a file holding what could not be sent; default in the temp dir). OPS_TOKEN and
OPS_URLS fall back to ~/.config/ops/env, which the Mac and the Pi both have.

Sending happens on one background thread, so a caller never waits on the network.
"""
import atexit
import json
import logging
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

def _machine_env():
    """~/.config/ops/env, written to the Mac and the Pi by bin/install-standby.sh, so a service there
    needs nothing in its own environment. A value set in the environment wins."""
    out = {}
    try:
        with open(os.path.expanduser("~/.config/ops/env")) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#"):
                    out[k] = v
    except OSError:
        pass
    return out


_ENV = {**_machine_env(), **os.environ}
URLS = [u.strip() for u in _ENV.get("OPS_URLS", "https://ops-responder.fly.dev").split(",") if u.strip()]
TOKEN = _ENV.get("OPS_TOKEN", "")
MACHINE = os.environ.get("OPS_MACHINE", socket.gethostname())
SERVICE = os.environ.get("OPS_SERVICE", os.path.basename(sys.argv[0] or "python"))
SPOOL = os.environ.get("OPS_SPOOL", "")

# The same message repeated inside a short window is sent once, with its count.
COALESCE_S = 30
_recent = {}
_q = queue.Queue()
_lock = threading.Lock()
_started = False


def _spool_path():
    return SPOOL or os.path.join(tempfile.gettempdir(), "ops-spool-%s.jsonl" % SERVICE.replace("/", "_"))


def _post(kind, items):
    if not TOKEN:
        return False
    body = json.dumps(items).encode()
    for u in URLS:
        try:
            req = urllib.request.Request("%s/%s" % (u, kind), data=body, method="POST", headers={
                "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                if 200 <= r.status < 300:
                    return True
        except Exception:
            continue
    return False


def _flush_spool():
    p = _spool_path()
    if not os.path.exists(p):
        return
    try:
        with open(p) as f:
            items = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return
    if not items or _post("error", items):
        try:
            os.remove(p)
        except OSError:
            pass


def _sender():
    while True:
        kind, item = _q.get()
        try:
            if _post(kind, [item]):
                _flush_spool()
            elif kind == "error":
                # A missed beat is not kept: the next one replaces it, and its absence is the signal.
                try:
                    with open(_spool_path(), "a") as f:
                        f.write(json.dumps(item) + "\n")
                except OSError:
                    pass
        finally:
            _q.task_done()


def flush(timeout=5.0):
    """Wait for what is queued to be sent (or spooled). Runs at exit on its own, because a short-lived
    script would otherwise end before the background thread had sent anything."""
    end = time.time() + timeout
    while _q.unfinished_tasks and time.time() < end:
        time.sleep(0.05)


def _ensure_thread():
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_sender, name="ops-report", daemon=True).start()
            _started = True


def report(err, context=None, stack=None):
    if isinstance(err, BaseException):
        message = "%s: %s" % (type(err).__name__, err)
        stack = stack or "".join(traceback.format_exception(type(err), err, err.__traceback__))
    else:
        message = str(err)
    key = "".join("N" if c.isdigit() else c for c in message.split("\n")[0])[:200]
    now = time.time()
    seen = _recent.get(key)
    if seen and now - seen["at"] < COALESCE_S:
        seen["count"] += 1
        return
    if seen and seen["count"] > 1:
        _q.put(("error", {"machine": MACHINE, "service": SERVICE, "count": seen["count"] - 1,
                          "message": "%s (x%d more in %ds)" % (seen["message"], seen["count"] - 1, now - seen["at"])}))
    _recent[key] = {"at": now, "count": 1, "message": message}
    if len(_recent) > 500:
        _recent.pop(next(iter(_recent)))
    _ensure_thread()
    _q.put(("error", {"machine": MACHINE, "service": SERVICE, "message": message[:4000], "stack": stack,
                      "context": context, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}))


def beat(every_s=60, info=None):
    _ensure_thread()
    _q.put(("beat", {"machine": MACHINE, "service": SERVICE, "every_s": every_s, "info": info}))


class _Handler(logging.Handler):
    def emit(self, record):
        try:
            if record.name.startswith("urllib3"):
                return
            stack = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            report(record.getMessage(), stack=stack)
        except Exception:
            pass


def _send_now(item):
    # A crashing process has no background thread left to wait for.
    if not _post("error", [item]):
        try:
            with open(_spool_path(), "a") as f:
                f.write(json.dumps(item) + "\n")
        except OSError:
            pass


def install(machine=None, service=None, spool=None):
    global MACHINE, SERVICE, SPOOL
    MACHINE, SERVICE, SPOOL = machine or MACHINE, service or SERVICE, spool or SPOOL
    logging.getLogger().addHandler(_Handler(level=logging.ERROR))
    prev = sys.excepthook

    def hook(t, e, tb):
        _send_now({"machine": MACHINE, "service": SERVICE, "message": "%s: %s" % (t.__name__, e),
                   "stack": "".join(traceback.format_exception(t, e, tb))})
        prev(t, e, tb)
    sys.excepthook = hook

    def thook(args):
        if args.exc_type is SystemExit:
            return
        report(args.exc_value or args.exc_type.__name__)
        sys.__stderr__.write("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    threading.excepthook = thook
    _ensure_thread()
    threading.Thread(target=_flush_spool, daemon=True).start()
    atexit.register(flush)
