#!/usr/bin/env python3
"""
Headless service monitor — live status page for the Claude services and
scheduled routines running on a headless workbench host.

Self-contained: stdlib only. Serves an auto-refreshing dashboard at / and the
underlying JSON at /api/status. Health is recomputed live on every /api/status
request, so the page always reflects the box's current state.

Run standalone:   python3 monitor.py
Managed:          systemctl --user start service-monitor.service
"""

import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
# Default to localhost — a safe default. Set MONITOR_BIND to a specific
# interface IP (e.g. a VPN/mesh address) to expose the page only there.
BIND = os.environ.get("MONITOR_BIND", "127.0.0.1")
PORT = int(os.environ.get("MONITOR_PORT", "8787"))
HOSTNAME = socket.gethostname()

# Control-plane restart state (see restart_serve). Kept out of git.
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
LAUNCH_SPEC = os.path.join(STATE_DIR, "serve-launch.json")
RESTART_STATE = os.path.join(STATE_DIR, "serve-restart.json")
RESTART_MAX = 3          # relaunch attempts allowed within the window
RESTART_WINDOW = 1800    # seconds — the backoff window

# Scheduled routines are defined here, not hardcoded (issue #3).
ROUTINES_CONF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "routines.toml")

# Alerting (issue #2). State + local alert log live under the gitignored state/.
ALERT_STATE = os.path.join(STATE_DIR, "alert-state.json")
ALERT_LOG = os.path.join(STATE_DIR, "alerts.log")
BAD = {"warn", "fail"}   # statuses that count as "a problem" for alerting

# A routine is "stale" once it is this far past its expected cadence.
DAY = 86400
RESEARCH_STALE_S = 26 * 3600   # daily → warn if last run older than 26h
BLOG_WARN_S = 26 * 3600        # daily → warn after 26h with no new post
BLOG_FAIL_S = 50 * 3600        # fail once two days have been missed

# Status severity ordering for rolling up an overall verdict.
SEVERITY = {"ok": 0, "info": 1, "running": 1, "warn": 2, "fail": 3, "unknown": 2}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def run(cmd, timeout=8):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def next_daily(hour, minute=0):
    """Epoch of the next local occurrence of hour:minute."""
    now = datetime.now()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt.timestamp()


def ps_snapshot():
    """List of (pid, etimes, pcpu, pmem, args) for all processes."""
    rc, out, _ = run(["ps", "-eo", "pid=,etimes=,pcpu=,pmem=,args="])
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        pid, etimes, pcpu, pmem, args = parts
        rows.append((pid, etimes, pcpu, pmem, args))
    return rows


def sctl_user_show(unit, props):
    """systemctl --user show for a unit -> dict of the requested props."""
    cmd = ["systemctl", "--user", "show", unit] + [f"-p{p}" for p in props]
    rc, out, _ = run(cmd)
    d = {}
    if rc == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def socket_path_from_args(args):
    """Extract the --socket <path> value from a command line, or None."""
    m = re.search(r"--socket\s+(\S+)", args)
    return m.group(1) if m else None


def unix_socket_alive(path):
    """True iff something is actually listening on the unix socket at `path`.

    This is the authoritative liveness signal for the control plane — it is how
    the platform's bootstrap decides whether to start a new server — and it is
    what makes restart safe: we only relaunch when nothing answers here.
    """
    if not path:
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2)
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _is_serve(args):
    """Match the serve daemon by its binary invocation, not a loose substring,
    so a command that merely *mentions* the pattern (a grep, this monitor's own
    args) is not miscounted as the daemon."""
    return "remote/srv/" in args and "/server --serve" in args


def _is_bridge(args):
    return "remote/srv/" in args and "/server --bridge" in args


def check_claude_services(procs):
    """The ccd remote control-plane daemons on this host.

    Only the `--serve` daemon is a persistent service (reparented to init), so it
    is the real control-plane health signal. The `--bridge` processes are spawned
    per remote SSH connection, so their absence just means "no one is connected"
    — that is reported as an active-connection count, never as a failure.
    """
    items = []

    # Persistent serve daemon — the actual control-plane health.
    serve = next((p for p in procs if _is_serve(p[4])), None)
    if serve:
        pid, etimes, pcpu, pmem, args = serve
        try:
            uptime = int(etimes)
        except ValueError:
            uptime = None
        sockpath = socket_path_from_args(args)
        listening = unix_socket_alive(sockpath) if sockpath else None
        status = "ok"
        summary = f"running · pid {pid}"
        if listening is False:
            status = "warn"
            summary = f"running · pid {pid} · rpc socket not responding"
        items.append({
            "id": "claude-remote-serve", "group": "Claude Services",
            "name": "Claude remote server", "status": status, "summary": summary,
            "detail": [
                {"label": "PID", "value": pid},
                {"label": "Uptime", "value": human_dur(uptime)},
                {"label": "CPU", "value": f"{pcpu}%"},
                {"label": "MEM", "value": f"{pmem}%"},
                {"label": "RPC socket", "value":
                    {True: "listening", False: "not responding"}.get(
                        listening, "unknown")},
            ],
        })
    else:
        items.append({
            "id": "claude-remote-serve", "group": "Claude Services",
            "name": "Claude remote server", "status": "fail",
            "summary": "not running",
            "detail": [{"label": "Process", "value": "no --serve daemon in ps"}],
        })

    # Bridges — one per active remote SSH connection; absent == idle, not down.
    bridges = [p for p in procs if _is_bridge(p[4])]
    n = len(bridges)
    detail = [{"label": "Active", "value": str(n)}]
    uptimes = [int(b[1]) for b in bridges if b[1].isdigit()]
    if uptimes:
        detail.append({"label": "Longest", "value": human_dur(max(uptimes))})
    items.append({
        "id": "claude-remote-connections", "group": "Claude Services",
        "name": "Remote connections", "status": "ok",
        "summary": (f"{n} active" if n else "idle · 0 connected"),
        "detail": detail,
        "note": "bridge processes are spawned per remote SSH connection",
    })
    return items


# --------------------------------------------------------------------------- #
# control-plane restart  (issue #4)
#
# The `server --serve` daemon is launched by the platform's SSH bootstrap and is
# not supervised by anything — if it dies it stays dead. There is no official
# restart command, so a relaunch means re-exec'ing the exact recorded argv.
#
# SAFETY INVARIANT: we only ever relaunch when the daemon is *provably down* —
# no `--serve` process AND nothing listening on its socket. We never kill or
# signal a running daemon. The worst case is therefore "couldn't revive a
# already-dead control plane", never "broke a working one".
# --------------------------------------------------------------------------- #
def _serve_procs(procs):
    return [p for p in procs if _is_serve(p[4])]


def capture_launch_spec(procs):
    """While the daemon is healthy, remember exactly how it was launched so we
    can reproduce it if it dies. Writes only when the argv changes."""
    serve = _serve_procs(procs)
    if not serve:
        return
    pid = serve[0][0]
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = [a.decode() for a in fh.read().split(b"\x00") if a]
    except OSError:
        return
    if not argv:
        return
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = HOME
    spec = {"argv": argv, "cwd": cwd,
            "sockpath": socket_path_from_args(" ".join(argv))}
    try:
        if os.path.exists(LAUNCH_SPEC):
            with open(LAUNCH_SPEC) as fh:
                if json.load(fh).get("argv") == argv:
                    return
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LAUNCH_SPEC, "w") as fh:
            json.dump(spec, fh, indent=2)
    except (OSError, ValueError):
        pass


def load_launch_spec():
    try:
        with open(LAUNCH_SPEC) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _recent_attempts(now):
    try:
        with open(RESTART_STATE) as fh:
            att = json.load(fh).get("attempts", [])
    except (OSError, ValueError):
        att = []
    return [t for t in att if now - t < RESTART_WINDOW]


def _record_attempt(now):
    att = _recent_attempts(now) + [now]
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(RESTART_STATE, "w") as fh:
            json.dump({"attempts": att}, fh)
    except OSError:
        pass


def _spawn_detached(argv, cwd, logpath):
    """Launch argv fully detached from this process, logging to logpath."""
    logf = open(logpath, "a") if logpath else subprocess.DEVNULL
    try:
        subprocess.Popen(
            argv, cwd=cwd or HOME,
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=os.environ.copy(),
        )
    finally:
        if logpath:
            logf.close()


def restart_serve(dry_run=False):
    """Relaunch the control-plane serve daemon iff it is provably down.

    Returns a result dict with an `action`: skip | error | throttled |
    would-restart | restarted, and `ok` (bool)."""
    procs = ps_snapshot()
    capture_launch_spec(procs)          # keep the spec fresh while healthy
    serve = _serve_procs(procs)
    spec = load_launch_spec()
    sockpath = (spec or {}).get("sockpath")
    alive = unix_socket_alive(sockpath) if sockpath else False

    # SAFETY INVARIANT: never touch a control plane that is up in any way.
    if serve or alive:
        return {"action": "skip", "ok": True,
                "reason": "control plane is healthy — refusing to touch it",
                "serve_procs": len(serve), "socket_alive": alive}
    if not spec:
        return {"action": "error", "ok": False,
                "reason": "no launch spec captured yet; let the platform start "
                          "the daemon once so it can be recorded"}

    now = time.time()
    recent = _recent_attempts(now)
    if len(recent) >= RESTART_MAX:
        return {"action": "throttled", "ok": False,
                "reason": f"{len(recent)} attempts within "
                          f"{RESTART_WINDOW // 60}m (cap {RESTART_MAX}) — "
                          "backing off"}
    if dry_run:
        return {"action": "would-restart", "ok": True,
                "argv": spec["argv"], "cwd": spec.get("cwd"),
                "sockpath": sockpath}

    # Remove a stale socket file so the fresh daemon can bind. Safe: we already
    # confirmed nothing is listening on it.
    try:
        if sockpath and os.path.exists(sockpath):
            os.unlink(sockpath)
    except OSError as exc:
        return {"action": "error", "ok": False,
                "reason": f"could not remove stale socket: {exc}"}

    logpath = (os.path.join(os.path.dirname(sockpath), "remote-server.log")
               if sockpath else None)
    _record_attempt(now)
    try:
        _spawn_detached(spec["argv"], spec.get("cwd"), logpath)
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "ok": False,
                "reason": f"relaunch failed: {exc}"}

    up = any(time.sleep(0.5) or unix_socket_alive(sockpath) for _ in range(10))
    return {"action": "restarted", "ok": up,
            "reason": ("socket is listening" if up else
                       "relaunched, but socket has not come up yet — check "
                       "the server log"),
            "sockpath": sockpath}


def watch_serve(interval):
    """Watchdog loop: probe the control plane and relaunch only when down."""
    print(f"serve-watchdog: probing every {interval}s "
          "(relaunches only when the daemon is down)", flush=True)
    while True:
        res = restart_serve()
        if res.get("action") != "skip":
            print(time.strftime("%Y-%m-%dT%H:%M:%S ") + json.dumps(res),
                  flush=True)
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# scheduled routines  (issue #3)
#
# Routines are defined in routines.toml, not hardcoded here. Each [[routine]]
# picks a `signal` mapped to one of the detectors below, so adding, editing, or
# removing a monitored routine is a config change, not a code change.
# --------------------------------------------------------------------------- #
def parse_duration(val, default=None):
    """Parse '26h', '90m', '2h30m', '45s', or a plain number of seconds."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if s.isdigit():
        return int(s)
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total, num = 0, ""
    for ch in s:
        if ch.isdigit():
            num += ch
        elif ch in units and num:
            total += int(num) * units[ch]
            num = ""
        else:
            raise ValueError(f"bad duration: {val!r}")
    if num:                              # trailing bare number == seconds
        total += int(num)
    return total


def _sched_next(schedule):
    """Epoch of the next local HH:MM occurrence, or None."""
    if not schedule:
        return None
    try:
        hh, mm = str(schedule).split(":")
        return next_daily(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _routine_card(cfg, status, summary, detail, last_run, next_run):
    return {
        "id": cfg["id"], "group": "Scheduled Routines",
        "name": cfg.get("name", cfg["id"]),
        "status": status, "summary": summary, "detail": detail,
        "last_run": last_run, "next_run": next_run, "note": cfg.get("note"),
    }


def load_routines():
    """Read routines.toml -> (list_of_routine_dicts, error_or_None)."""
    try:
        with open(ROUTINES_CONF, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return [], f"routines.toml not found ({ROUTINES_CONF})"
    except tomllib.TOMLDecodeError as exc:
        return [], f"invalid TOML: {exc}"
    except OSError as exc:
        return [], str(exc)
    routines = data.get("routine", [])
    if not isinstance(routines, list):
        return [], "expected an array of [[routine]] tables"
    return routines, None


def build_routine_checks(procs):
    """Build the Scheduled Routines cards from config. A broken config, or one
    bad routine, degrades to an error card instead of taking down the page."""
    routines, err = load_routines()
    if err:
        return [{"id": "routines-config", "group": "Scheduled Routines",
                 "name": "routine config", "status": "fail",
                 "summary": "config error",
                 "detail": [{"label": "routines.toml", "value": err}]}]
    out = []
    for cfg in routines:
        rid = cfg.get("id", "?")
        try:
            out.append(check_routine(cfg, procs))
        except Exception as exc:  # noqa: BLE001 — isolate a bad routine
            out.append({"id": rid, "group": "Scheduled Routines",
                        "name": rid, "status": "warn",
                        "summary": "check error",
                        "detail": [{"label": "Error", "value": str(exc)}]})
    return out


def check_routine(cfg, procs):
    sig = cfg.get("signal")
    if sig == "systemd-timer":
        return _signal_systemd_timer(cfg)
    if sig == "git-commit-age":
        return _signal_git_commit_age(cfg)
    if sig == "process-match":
        return _signal_process_match(cfg, procs)
    return _routine_card(cfg, "fail", f"unknown signal: {sig!r}",
                         [{"label": "signal", "value": str(sig)}], None,
                         _sched_next(cfg.get("schedule")))


def _signal_systemd_timer(cfg):
    """A routine driven by a systemd timer/service with an rc-marked log."""
    svc = sctl_user_show(
        cfg["service"],
        ["ActiveState", "SubState", "Result", "ExecMainStatus"])
    unit = cfg.get("unit")
    timer_active = (run(["systemctl", "--user", "is-active", unit])[1]
                    if unit else "n/a")
    running = (svc.get("ActiveState") == "activating"
               or svc.get("SubState") in ("start", "running"))

    last_epoch = last_rc = log_name = log_size = None
    logs = (sorted(glob.glob(os.path.expanduser(cfg["log_glob"])))
            if cfg.get("log_glob") else [])
    if logs:
        newest = logs[-1]
        log_name = os.path.basename(newest)
        try:
            log_size = os.path.getsize(newest)
            last_epoch = os.path.getmtime(newest)
        except OSError:
            pass
        try:
            with open(newest, "r", errors="replace") as fh:
                tail = fh.read()[-4000:]
            m = list(re.finditer(r"run finished rc=(\d+) at (\S+)", tail))
            if m:
                last_rc = int(m[-1].group(1))
                try:
                    last_epoch = datetime.fromisoformat(
                        m[-1].group(2)).timestamp()
                except ValueError:
                    pass
        except OSError:
            pass

    age = (time.time() - last_epoch) if last_epoch else None
    stale = parse_duration(cfg.get("stale_after"), RESEARCH_STALE_S)

    if running:
        status, summary = "running", "run in progress"
    elif unit and timer_active != "active":
        status, summary = "fail", f"timer {timer_active or 'inactive'}"
    elif last_rc not in (0, None):
        status, summary = "fail", f"last run exited rc={last_rc}"
    elif svc.get("Result") not in ("success", "", None):
        status, summary = "fail", f"service result: {svc.get('Result')}"
    elif last_epoch is None:
        status, summary = "warn", "no run recorded yet"
    elif age > stale:
        status, summary = "warn", f"stale — last run {human_dur(age)} ago"
    else:
        status, summary = "ok", f"last run {human_dur(age)} ago"

    detail = [
        {"label": "Timer", "value": timer_active or "unknown"},
        {"label": "Service", "value":
            f"{svc.get('ActiveState','?')}/{svc.get('SubState','?')}"},
        {"label": "Last result", "value":
            ("rc=%d" % last_rc) if last_rc is not None else
            svc.get("Result", "unknown")},
        {"label": "Log", "value":
            f"{log_name} ({log_size}B)" if log_name else "none"},
    ]
    return _routine_card(cfg, status, summary, detail, last_epoch,
                         _sched_next(cfg.get("schedule")))


def _signal_git_commit_age(cfg):
    """A routine whose success shows up as a fresh commit in a git repo."""
    repo = os.path.expanduser(cfg["repo"])
    grep = cfg.get("grep")
    base = ["git", "-C", repo, "log", "-1", "--format=%ct%x09%s"]
    if grep:
        rc, out, _ = run(base[:5] + [f"--grep={grep}"] + base[5:])
        if rc != 0 or not out:           # fall back to newest commit of any kind
            rc, out, _ = run(base)
    else:
        rc, out, _ = run(base)
    last_epoch, subject = None, None
    if rc == 0 and out and "\t" in out:
        ep, subject = out.split("\t", 1)
        try:
            last_epoch = float(ep)
        except ValueError:
            pass

    age = (time.time() - last_epoch) if last_epoch else None
    warn = parse_duration(cfg.get("warn_after"), BLOG_WARN_S)
    fail = parse_duration(cfg.get("fail_after"), BLOG_FAIL_S)
    if last_epoch is None:
        status, summary = "unknown", "no commits found in repo"
    elif age > fail:
        status, summary = "fail", f"no commit in {human_dur(age)}"
    elif age > warn:
        status, summary = "warn", f"last commit {human_dur(age)} ago"
    else:
        status, summary = "ok", f"last commit {human_dur(age)} ago"

    detail = [
        {"label": "Last commit", "value": (subject[:60] if subject else "n/a")},
        {"label": "Signal", "value":
            f"{os.path.basename(repo)} git HEAD (local clone)"},
    ]
    return _routine_card(cfg, status, summary, detail, last_epoch,
                         _sched_next(cfg.get("schedule")))


def _signal_process_match(cfg, procs):
    """A routine that is itself a long-running process; up/down by ps match."""
    subs = cfg.get("match") if isinstance(cfg.get("match"), list) \
        else [cfg.get("match")]
    hit = next((p for p in procs if all(s in p[4] for s in subs if s)), None)
    if hit:
        pid, etimes, pcpu, pmem, _ = hit
        try:
            uptime = int(etimes)
        except ValueError:
            uptime = None
        status, summary = "ok", f"running · pid {pid}"
        detail = [
            {"label": "PID", "value": pid},
            {"label": "Uptime", "value": human_dur(uptime)},
            {"label": "CPU", "value": f"{pcpu}%"},
            {"label": "MEM", "value": f"{pmem}%"},
        ]
    else:
        status, summary = "fail", "not running"
        detail = [{"label": "Process", "value": "no match in ps"}]
    return _routine_card(cfg, status, summary, detail, None,
                         _sched_next(cfg.get("schedule")))


def human_dur(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < DAY:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // DAY}d {(seconds % DAY) // 3600}h"


def gather():
    procs = ps_snapshot()
    capture_launch_spec(procs)          # remember how the daemon was launched
    checks = check_claude_services(procs)
    checks.extend(build_routine_checks(procs))
    overall = "ok"
    for c in checks:
        if SEVERITY.get(c["status"], 2) > SEVERITY.get(overall, 0):
            overall = c["status"]
    return {
        "host": HOSTNAME,
        "generated_at": time.time(),
        "overall": overall,
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# alerting  (issue #2)
#
# A separate evaluator (run by a systemd timer) diffs each check's status against
# saved state and notifies ONLY on transitions across the warn/fail boundary —
# never on every poll. It runs gather() in-process, so it keeps working even if
# the web server is down. Delivery is pluggable and configured via env (secrets
# in a gitignored EnvironmentFile); with nothing configured it logs to a file.
# --------------------------------------------------------------------------- #
def _load_alert_state():
    try:
        with open(ALERT_STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None   # no baseline yet


def _save_alert_state(statuses):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ALERT_STATE, "w") as fh:
            json.dump({"statuses": statuses, "updated": time.time()}, fh,
                      indent=2)
    except OSError:
        pass


def evaluate_alerts(checks, prev):
    """Pure diff: given current checks and prev {id: status}, return
    (transitions, new_statuses). Fires when a check crosses the warn/fail
    boundary in either direction; silent on the first run (prev is None)."""
    new = {c["id"]: c["status"] for c in checks}
    if prev is None:
        return [], new                       # establish baseline silently
    by_id = {c["id"]: c for c in checks}
    transitions = []
    for cid, cur in new.items():
        was = prev.get(cid, "ok")            # a newly-seen check compares to ok
        if cur != was and (cur in BAD or was in BAD):
            kind = ("recovered" if cur not in BAD
                    else "escalated" if was in BAD else "problem")
            transitions.append({
                "id": cid, "from": was, "to": cur, "kind": kind,
                "summary": by_id[cid].get("summary", ""),
                "group": by_id[cid].get("group", ""),
            })
    return transitions, new


def _fmt_alert(transitions, host):
    n_bad = sum(1 for t in transitions if t["to"] in BAD)
    n_ok = len(transitions) - n_bad
    parts = ([f"{n_bad} problem{'s' if n_bad != 1 else ''}"] if n_bad else []) \
        + ([f"{n_ok} recovered"] if n_ok else [])
    subject = f"[{host}] monitor: " + ", ".join(parts)
    lines = [f"  [{t['to']}] {t['id']}: {t['summary']}  ({t['from']} -> "
             f"{t['to']})" for t in transitions]
    return subject, subject + "\n" + "\n".join(lines)


def _notify_log(subject, body):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ALERT_LOG, "a") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + body + "\n\n")
    except OSError:
        pass


def _notify_webhook(subject, body, transitions):
    url = os.environ.get("MONITOR_ALERT_WEBHOOK")
    if not url:
        return
    import urllib.request
    data = json.dumps({"text": body, "subject": subject,
                       "transitions": transitions}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:  # noqa: BLE001
        print(f"alert: webhook failed: {exc}", file=sys.stderr)


def _notify_email(subject, body):
    host = os.environ.get("MONITOR_ALERT_SMTP_HOST")
    to = os.environ.get("MONITOR_ALERT_EMAIL_TO")
    if not host or not to:
        return
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MONITOR_ALERT_EMAIL_FROM", f"monitor@{HOSTNAME}")
    msg["To"] = to
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("MONITOR_ALERT_SMTP_PORT",
                                                    "25")), timeout=15) as s:
            if os.environ.get("MONITOR_ALERT_SMTP_STARTTLS"):
                s.starttls()
            user = os.environ.get("MONITOR_ALERT_SMTP_USER")
            if user:
                s.login(user, os.environ.get("MONITOR_ALERT_SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        print(f"alert: email failed: {exc}", file=sys.stderr)


def _heartbeat():
    """Dead-man's switch: ping an external URL each run so an outside service
    notices if this evaluator stops (answers 'who watches the watcher')."""
    url = os.environ.get("MONITOR_ALERT_HEARTBEAT_URL")
    if not url:
        return
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=10).close()
    except Exception as exc:  # noqa: BLE001
        print(f"alert: heartbeat failed: {exc}", file=sys.stderr)


def run_alert_cycle(dry_run=False):
    state = _load_alert_state()
    prev = state.get("statuses") if state else None
    data = gather()
    transitions, new = evaluate_alerts(data["checks"], prev)
    if transitions and not dry_run:
        subject, body = _fmt_alert(transitions, data["host"])
        _notify_log(subject, body)
        _notify_webhook(subject, body, transitions)
        _notify_email(subject, body)
    if not dry_run:
        _save_alert_state(new)
        _heartbeat()
    return {"baseline": prev is None, "overall": data["overall"],
            "transitions": transitions}


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/status"):
            self._send(200, json.dumps(gather(), indent=2),
                       "application/json")
        elif self.path in ("/healthz", "/health"):
            self._send(200, "ok\n", "text/plain")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found\n", "text/plain")

    def log_message(self, *args):  # silence default stderr logging
        pass


PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>workbench · service monitor</title>
<style>
:root{
  --bg:#0d1117;--panel:#161b22;--edge:#30363d;--txt:#e6edf3;--dim:#8b949e;
  --ok:#3fb950;--warn:#d29922;--fail:#f85149;--run:#58a6ff;--info:#58a6ff;--unknown:#8b949e;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  background:var(--bg);color:var(--txt)}
.wrap{max-width:920px;margin:0 auto;padding:28px 18px 60px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:18px;margin:0;font-weight:600}
.host{color:var(--dim)}
.pill{margin-left:auto;padding:6px 14px;border-radius:20px;font-weight:700;
  text-transform:uppercase;letter-spacing:.05em;font-size:12px;color:#0d1117}
.pill.ok{background:var(--ok)}.pill.warn{background:var(--warn)}
.pill.fail{background:var(--fail)}.pill.running,.pill.info{background:var(--run)}
.pill.unknown{background:var(--unknown)}
.group{margin:26px 0 10px;font-size:12px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--dim)}
.card{background:var(--panel);border:1px solid var(--edge);border-left-width:4px;
  border-radius:8px;padding:14px 16px;margin-bottom:10px}
.card.ok{border-left-color:var(--ok)}.card.warn{border-left-color:var(--warn)}
.card.fail{border-left-color:var(--fail)}.card.running,.card.info{border-left-color:var(--run)}
.card.unknown{border-left-color:var(--unknown)}
.row1{display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;flex:0 0 auto}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.dot.fail{background:var(--fail)}.dot.running,.dot.info{background:var(--run);
  box-shadow:0 0 0 0 var(--run);animation:pulse 1.6s infinite}
.dot.unknown{background:var(--unknown)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(88,166,255,.5)}
  70%{box-shadow:0 0 0 7px rgba(88,166,255,0)}100%{box-shadow:0 0 0 0 rgba(88,166,255,0)}}
.name{font-weight:600}
.summary{margin-left:auto;color:var(--dim)}
.detail{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:9px;color:var(--dim);font-size:12.5px}
.detail b{color:var(--txt);font-weight:500}
.note{margin-top:7px;font-size:11.5px;color:#6e7681}
.next{margin-top:6px;font-size:12px;color:var(--dim)}
footer{margin-top:30px;color:#6e7681;font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
#err{display:none;background:var(--fail);color:#0d1117;padding:8px 14px;border-radius:6px;font-weight:600}
</style></head><body><div class="wrap">
<header>
  <h1>service monitor</h1>
  <span class="host" id="host">workbench</span>
  <span class="pill unknown" id="overall">…</span>
</header>
<div id="err">monitor unreachable — retrying…</div>
<div id="body"></div>
<footer>
  <span id="updated">loading…</span>
  <span>auto-refresh 12s</span>
  <span><a href="/api/status" style="color:#6e7681">/api/status</a></span>
</footer>
</div><script>
const S={ok:'ok',warn:'warn',fail:'fail',running:'running',info:'info',unknown:'unknown'};
function dur(s){if(s==null)return'unknown';s=Math.abs(Math.round(s));
  if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';
  if(s<86400)return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';
  return Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h';}
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}
function card(c){
  const st=S[c.status]||'unknown';
  let d=c.detail.map(x=>`<span><b>${esc(x.label)}</b> ${esc(x.value)}</span>`).join('');
  let nxt='';
  if(c.next_run){const rel=c.next_run-Date.now()/1000;
    nxt=`<div class="next">next expected in ${dur(rel)}</div>`;}
  let note=c.note?`<div class="note">${esc(c.note)}</div>`:'';
  return `<div class="card ${st}">
    <div class="row1"><span class="dot ${st}"></span>
      <span class="name">${esc(c.name)}</span>
      <span class="summary">${esc(c.summary)}</span></div>
    <div class="detail">${d}</div>${nxt}${note}</div>`;
}
async function tick(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    const j=await r.json();
    document.getElementById('err').style.display='none';
    document.getElementById('host').textContent=j.host;
    const ov=document.getElementById('overall');
    ov.className='pill '+(S[j.overall]||'unknown');ov.textContent=j.overall;
    const groups={};j.checks.forEach(c=>{(groups[c.group]=groups[c.group]||[]).push(c)});
    let html='';for(const g in groups){html+=`<div class="group">${esc(g)}</div>`;
      groups[g].forEach(c=>html+=card(c));}
    document.getElementById('body').innerHTML=html;
    const ageS=Math.round(Date.now()/1000-j.generated_at);
    document.getElementById('updated').textContent='updated '+dur(ageS)+' ago';
  }catch(e){document.getElementById('err').style.display='block';}
}
tick();setInterval(tick,12000);
</script></body></html>"""


class MonitorServer(ThreadingHTTPServer):
    """HTTP server that can bind an interface address before it exists.

    IP_FREEBIND lets us bind e.g. a VPN/mesh IP even if that interface comes up
    after this service at boot, instead of crash-looping until it appears."""
    allow_reuse_address = True

    def server_bind(self):
        freebind = getattr(socket, "IP_FREEBIND", 15)  # Linux value
        try:
            self.socket.setsockopt(socket.IPPROTO_IP, freebind, 1)
        except OSError:
            pass
        super().server_bind()


def run_web():
    srv = MonitorServer((BIND, PORT), Handler)
    print(f"service-monitor listening on http://{BIND}:{PORT} "
          f"(host {HOSTNAME})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


USAGE = ("usage: monitor.py [serve | status | alert [--dry-run] | "
         "restart-serve [--dry-run] | watch [--interval=N]]")


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "serve"

    if cmd in ("serve", "web"):
        run_web()
    elif cmd == "status":
        print(json.dumps(gather(), indent=2))
    elif cmd == "alert":
        print(json.dumps(run_alert_cycle(dry_run="--dry-run" in argv),
                         indent=2))
    elif cmd == "restart-serve":
        res = restart_serve(dry_run="--dry-run" in argv)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)
    elif cmd == "watch":
        interval = 30
        for a in argv[1:]:
            if a.startswith("--interval="):
                interval = int(a.split("=", 1)[1])
        watch_serve(interval)
    else:
        print(USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
