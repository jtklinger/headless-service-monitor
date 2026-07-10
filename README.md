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
