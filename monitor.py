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
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
BIND = os.environ.get("MONITOR_BIND", "0.0.0.0")
PORT = int(os.environ.get("MONITOR_PORT", "8787"))
HOSTNAME = socket.gethostname()

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
def rpc_socket_present(args):
    """True/False if the daemon's --socket path exists; None if unparseable."""
    m = re.search(r"--socket\s+(\S+)", args)
    if not m:
        return None
    try:
        return os.path.exists(m.group(1))
    except OSError:
        return None


def check_claude_services(procs):
    """The ccd remote control-plane daemons on this host.

    Only the `--serve` daemon is a persistent service (reparented to init), so it
    is the real control-plane health signal. The `--bridge` processes are spawned
    per remote SSH connection, so their absence just means "no one is connected"
    — that is reported as an active-connection count, never as a failure.
    """
    items = []

    # Persistent serve daemon — the actual control-plane health.
    serve = next((p for p in procs
                  if "remote/srv/" in p[4] and " --serve" in p[4]), None)
    if serve:
        pid, etimes, pcpu, pmem, args = serve
        try:
            uptime = int(etimes)
        except ValueError:
            uptime = None
        sock_ok = rpc_socket_present(args)
        status = "ok"
        summary = f"running · pid {pid}"
        if sock_ok is False:
            status, summary = "warn", f"running · pid {pid} · rpc socket missing"
        items.append({
            "id": "claude-remote-serve", "group": "Claude Services",
            "name": "Claude remote server", "status": status, "summary": summary,
            "detail": [
                {"label": "PID", "value": pid},
                {"label": "Uptime", "value": human_dur(uptime)},
                {"label": "CPU", "value": f"{pcpu}%"},
                {"label": "MEM", "value": f"{pmem}%"},
                {"label": "RPC socket", "value":
                    {True: "present", False: "missing"}.get(sock_ok, "unknown")},
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
    bridges = [p for p in procs
               if "remote/srv/" in p[4] and " --bridge" in p[4]]
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


def check_daily_research():
    """systemd user timer/service on workbench + its log output."""
    svc = sctl_user_show(
        "daily-research.service",
        ["ActiveState", "SubState", "Result", "ExecMainStatus"],
    )
    timer_active = run(["systemctl", "--user", "is-active",
                        "daily-research.timer"])[1]

    running = (svc.get("ActiveState") == "activating"
               or svc.get("SubState") in ("start", "running"))

    # Newest log + its rc marker.
    logs = sorted(glob.glob(f"{HOME}/routines/logs/daily-research-*.log"))
    last_epoch, last_rc, log_name, log_size = None, None, None, None
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
            m = list(re.finditer(
                r"run finished rc=(\d+) at (\S+)", tail))
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

    if running:
        status, summary = "running", "run in progress"
    elif timer_active != "active":
        status, summary = "fail", f"timer {timer_active or 'inactive'}"
    elif last_rc not in (0, None):
        status, summary = "fail", f"last run exited rc={last_rc}"
    elif svc.get("Result") not in ("success", "", None):
        status, summary = "fail", f"service result: {svc.get('Result')}"
    elif last_epoch is None:
        status, summary = "warn", "no run recorded yet"
    elif age > RESEARCH_STALE_S:
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
    return {
        "id": "daily-research", "group": "Scheduled Routines",
        "name": "daily-research", "status": status, "summary": summary,
        "detail": detail,
        "last_run": last_epoch, "next_run": next_daily(22, 2),
        "note": "systemd user timer on workbench · nightly 22:02",
    }


def check_daily_blog():
    """Cloud agent — no local runner. Health inferred from blog git commits."""
    repo = f"{HOME}/projects/iter8lab.net"
    rc, out, _ = run(["git", "-C", repo, "log", "-1",
                      "--grep=^Add:", "--format=%ct%x09%s"])
    if rc != 0 or not out:
        rc, out, _ = run(["git", "-C", repo, "log", "-1", "--format=%ct%x09%s"])
    last_epoch, subject = None, None
    if rc == 0 and out and "\t" in out:
        ep, subject = out.split("\t", 1)
        try:
            last_epoch = float(ep)
        except ValueError:
            pass

    age = (time.time() - last_epoch) if last_epoch else None
    if last_epoch is None:
        status, summary = "unknown", "no commits found in blog repo"
    elif age > BLOG_FAIL_S:
        status, summary = "fail", f"no post in {human_dur(age)}"
    elif age > BLOG_WARN_S:
        status, summary = "warn", f"last post {human_dur(age)} ago"
    else:
        status, summary = "ok", f"last post {human_dur(age)} ago"

    detail = [
        {"label": "Last post", "value":
            (subject[:60] if subject else "n/a")},
        {"label": "Signal", "value": "iter8lab.net git HEAD (local clone)"},
    ]
    return {
        "id": "daily-blog-post", "group": "Scheduled Routines",
        "name": "daily-blog-post", "status": status, "summary": summary,
        "detail": detail,
        "last_run": last_epoch, "next_run": next_daily(23, 2),
        "note": "cloud agent (off-host) · publishes ~23:00 · health from commits",
    }


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
    checks = check_claude_services(procs)
    checks.append(check_daily_research())
    checks.append(check_daily_blog())
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


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"service-monitor listening on http://{BIND}:{PORT} "
          f"(host {HOSTNAME})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
