# headless-service-monitor

A tiny, dependency-free status page for a headless host that runs **Claude Code
services** and **scheduled routines**. It answers one question at a glance: *is
everything that's supposed to be running actually running, and did the nightly
jobs succeed?*

Pure Python standard library — no `pip install`, no framework. One file
(`monitor.py`) serves an auto-refreshing dashboard and a JSON API. Health is
recomputed live on every request, so the page always reflects the box's current
state.

## What it checks

| Check | Signal |
|-------|--------|
| **Claude remote server** (`server --serve`) | matched in `ps` → up/down, PID, uptime, CPU, mem |
| **Claude remote bridge** (`server --bridge`) | matched in `ps` |
| **daily-research** routine | its systemd `--user` timer/service state **plus** the `rc=N` marker in `~/routines/logs/` — distinguishes *running*, *stale/missed*, and *failed* |
| **daily-blog-post** routine | an off-host cloud agent with no local runner, so health is inferred from git commit age in its publish repo |

Each check reports `ok` / `warn` / `fail` / `running`, and the page rolls those
up into an overall verdict.

## Run it

```bash
python3 monitor.py          # serves http://0.0.0.0:8787
```

Then open `http://<host>:8787/`.

Environment overrides:

| Var | Default | Meaning |
|-----|---------|---------|
| `MONITOR_BIND` | `0.0.0.0` | interface to bind |
| `MONITOR_PORT` | `8787` | port |

## Endpoints

- `GET /` — auto-refreshing HTML dashboard (polls every 12s)
- `GET /api/status` — the same data as JSON, computed live
- `GET /healthz` — `200 ok` liveness for the monitor itself

## Run as a systemd user service

Copy `service-monitor.service` to `~/.config/systemd/user/`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now service-monitor.service
```

With user lingering enabled (`loginctl enable-linger $USER`) it survives logout
and reboots.

## Control-plane restart

The `--serve` daemon that connects this host to the platform is launched by the
platform's SSH bootstrap and is supervised by nothing — if it dies, it stays
dead. The monitor can relaunch it.

**Safety invariant:** a relaunch happens *only when the daemon is provably down*
— no `--serve` process **and** nothing listening on its RPC socket. The monitor
never kills or signals a running daemon, so it cannot make a healthy control
plane worse; the worst case is "couldn't revive an already-dead one". While the
daemon is healthy the monitor records its exact launch command, so a relaunch
reproduces the real invocation.

```bash
python3 monitor.py restart-serve --dry-run   # report what it would do
python3 monitor.py restart-serve             # relaunch iff down (else: skip)
python3 monitor.py watch --interval=30       # watchdog: probe + relaunch when down
python3 monitor.py status                    # print the JSON status once
```

Relaunches are rate-limited (default 3 per 30 min) to avoid crash loops.

### Automatic restart (opt-in)

`serve-watchdog.service` is a **template** — copy it to
`~/.config/systemd/user/` and `enable --now` to make restart automatic. Because
the relaunch mechanics cannot be fully tested without letting the live daemon
die, validate a real relaunch in a controlled window (where you can restore the
connection locally if needed) before relying on it unattended.

There is intentionally **no web trigger** for restart yet — a mutating endpoint
on a network-exposed page is exactly the exposure risk tracked in the issues.

## Adapting the checks

The checks are wired for one specific host. To add, remove, or retarget a check,
edit the `check_*` functions and the `gather()` roll-up in `monitor.py` — each
returns a plain dict, so a new check is just another function appended to the
list.

## Security note

By default the server binds `0.0.0.0`, which exposes the page to anything that
can route to the host. The data is low-sensitivity (process names and run
status), but you should still restrict access — bind to a private interface or
localhost, or add a firewall rule scoping the port. See the repo issues.

## License

MIT
