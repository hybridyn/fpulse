"""
F-Pulse CLI — command-line interface for pipeline management.

Commands:
  fpulse serve          Start the F-Pulse server
  fpulse run <id>       Execute a pipeline by ID
  fpulse list           List all pipelines
  fpulse status <id>    Check pipeline execution status
  fpulse logs <id>      View execution logs
  fpulse export <id>    Export pipeline as JSON
  fpulse import <file>  Import pipeline from JSON
  fpulse deploy <id>    Deploy pipeline to production
  fpulse health         Check server health
  fpulse doctor         Diagnose local runtime/port/service health (--repair)
  fpulse version        Show version info
  fpulse backup         Snapshot FPULSE_DATA_DIR to a tarball
  fpulse restore <src>  Restore a snapshot tarball
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    return os.environ.get("FPULSE_URL", "http://localhost:8001")


def _api_request(method: str, path: str, data: dict | None = None, token: str | None = None) -> dict | list:
    """Make an HTTP request to the F-Pulse API."""
    import urllib.request
    import urllib.error

    url = f"{_get_base_url()}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        print(f"Error {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection failed: {e.reason}", file=sys.stderr)
        print(f"Is F-Pulse running at {_get_base_url()}?", file=sys.stderr)
        sys.exit(1)


def _format_time(ts: float | str | None) -> str:
    if not ts:
        return "—"
    if isinstance(ts, str):
        return ts[:19]
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _print_table(rows: list[dict], columns: list[tuple[str, str, int]]):
    """Print a simple ASCII table."""
    header = "  ".join(f"{label:<{width}}" for _, label, width in columns)
    print(header)
    print("─" * len(header))
    for row in rows:
        line = "  ".join(f"{str(row.get(key, ''))[:width]:<{width}}" for key, _, width in columns)
        print(line)


# ── Commands ─────────────────────────────────────────────────────────

def cmd_serve(args):
    """Start the F-Pulse server.

    Default bind: ``127.0.0.1`` (loopback only). LAN exposure requires
    explicit opt-in via ``--host 0.0.0.0`` or env vars
    ``FPULSE_BIND_HOST`` / ``FPULSE_ALLOW_LAN=1``. See
    docs/install/security-hardening.md for the rationale.

    2026-06-02: when ``--open`` is passed, the launcher auto-finds a
    free port (8001 + fallback up to 10 ports) and opens the default
    browser. Skips browser auto-launch cleanly in WSL2 / DevContainer /
    SSH / headless-Linux environments so the operator can paste the
    URL into a real browser on their host.
    """
    import uvicorn
    from fpulse.cli.launcher import find_free_port, launch_browser_if_possible

    requested_port = args.port or int(os.environ.get("FPULSE_PORT", "8001"))
    # Resolution order: CLI flag > env override > convenience flag > safe default
    if args.host:
        host = args.host
    elif os.environ.get("FPULSE_BIND_HOST"):
        host = os.environ["FPULSE_BIND_HOST"].strip()
    elif os.environ.get("FPULSE_ALLOW_LAN", "").strip() in {"1", "true", "yes", "on"}:
        host = "0.0.0.0"
    else:
        host = "127.0.0.1"

    # Port-fallback only kicks in for launcher mode (--open) on the
    # loopback bind. Manual `fpulse serve` without --open keeps the
    # historical fail-fast behaviour so script-driven setups don't
    # silently bind a different port than the caller expects.
    if getattr(args, "open", False) and host == "127.0.0.1":
        try:
            actual_port = find_free_port(requested_port, host=host)
        except RuntimeError as exc:
            print(f"\n[ERROR] {exc}\n")
            return
        if actual_port != requested_port:
            print(
                f"  [note] port {requested_port} was in use — "
                f"using {actual_port} instead. Set --port explicitly to fail-fast."
            )
    else:
        actual_port = requested_port

    _loopback = (
        host in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")
        or host.startswith("127.")
    )
    if _loopback:
        url = f"http://{host}:{actual_port}"
        if not getattr(args, "open", False):
            print(f"Starting F-Pulse on {url} (loopback only — safe)")
    else:
        # Non-loopback bind = network-reachable. In LOCAL mode the API
        # allows anonymous access (single-user convenience), so exposing
        # it to a network is unsafe. Refuse unless the operator opts into
        # server mode (which requires auth). FPULSE_ALLOW_LAN=1 already
        # implies server mode; a raw --host 0.0.0.0 while still in local
        # mode is the dangerous combination we block here.
        from fpulse import runtime_config as _rc
        if _rc.IS_LOCAL_MODE:
            print(
                f"\n[SECURITY] Refusing to start: bind host {host!r} is "
                "network-reachable but security mode is 'local', which "
                "allows ANONYMOUS access (no login).\n"
                "  Exposing local mode to a network lets anyone reach "
                "uploads, backfills, AI actions and your data.\n"
                "  To run on a network safely, enable server mode (adds "
                "auth):\n"
                "      Windows    : set FPULSE_SECURITY_MODE=server\n"
                "      Linux/macOS: export FPULSE_SECURITY_MODE=server\n"
                "  …or keep it private with: --host 127.0.0.1\n"
            )
            return
        url = f"http://{host}:{actual_port}"
        print(
            f"\n[WARNING] Starting F-Pulse on {host}:{actual_port} in SERVER "
            "mode — network-reachable and requiring login. Put TLS / a "
            "reverse proxy in front for production.\n"
        )

    # 2026-06-07 — write the runtime ownership file BEFORE uvicorn
    # starts (uvicorn.run blocks). We don't yet know the listener PID;
    # for "open" mode the listener IS this launcher process, so we
    # record os.getpid(). `fpulse stop` reads this file and applies a
    # 3-signal ownership check before stopping anything.
    runtime_path = None
    try:
        from fpulse.cli.runtime_state import make_open_instance, write_runtime, remove_runtime
        instance = make_open_instance(host=host, port=actual_port, pid=os.getpid())
        runtime_path = write_runtime(instance)
        if getattr(args, "open", False):
            print(f"  Runtime state: {runtime_path}  (run `fpulse stop` for a clean shutdown)")
    except Exception as exc:
        # Runtime-file write is best-effort - never block the actual
        # server start on a state-file hiccup.
        print(f"  [note] could not write runtime state file: {exc}")
        runtime_path = None

    # `fpulse open` is the EPHEMERAL dev launcher: this process IS the
    # server, so enable the tab-close auto-shutdown (closing the window then
    # cleans up the server and frees port 8001). The always-on SERVICE runs
    # `uvicorn fpulse.main:app` directly and never sets this, so the service
    # survives the app window closing. See api/local_hardening.graceful_shutdown.
    if getattr(args, "open", False):
        os.environ["FPULSE_ALLOW_TAB_SHUTDOWN"] = "1"

    # Auto-launch browser before uvicorn.run() (which blocks). The
    # browser will retry until the backend is up; modern browsers
    # auto-reconnect within ~1s.
    if getattr(args, "open", False):
        launch_browser_if_possible(
            url,
            force_no_open=getattr(args, "no_open", False),
        )

    try:
        uvicorn.run("fpulse.main:app", host=host, port=actual_port, reload=args.reload)
    finally:
        # Clean exit (Ctrl+C, uvicorn shutdown, etc.) - remove the
        # ownership file so the next `fpulse open` doesn't think a
        # zombie is still running. Best-effort.
        try:
            from fpulse.cli.runtime_state import remove_runtime
            remove_runtime()
        except Exception:
            pass


def cmd_open(args):
    """`fpulse open` — alias for `fpulse serve --open`.

    This is the 1.0 "one-command local launch" verb. Equivalent to
    `fpulse serve --open` with all defaults preserved. Provided
    separately because it's the verb that's friendly to put in
    README / docs / desktop-shortcut targets — "open" is what
    non-technical operators expect to type or click.
    """
    # Synthesise the args shape `cmd_serve` expects, with --open on.
    class _OpenArgs:
        port = getattr(args, "port", None)
        host = getattr(args, "host", None)
        reload = False
        open = True
        no_open = getattr(args, "no_open", False)
    cmd_serve(_OpenArgs())


def cmd_app(args):
    """`fpulse app` — open the F-Pulse UI in an app-mode window WITHOUT
    starting a server.

    This is the verb the installed Start-Menu / desktop shortcut runs. The
    background service already serves the port, so we don't start a second
    server (that would collide on the port + the DuckDB file lock). We just
    open a window pointed at it.

    Preference order:
      1. A real NATIVE window via pywebview (WebView2) — resizable, sized to
         fit the screen, no browser chrome. Best first impression.
      2. A Chromium ``--app`` window (chromeless, isolated profile).
      3. A normal browser tab.
    """
    from fpulse.cli.launcher import (
        open_native_window, open_app_window, wait_for_server,
    )

    import time as _time

    port = getattr(args, "port", None) or int(os.environ.get("FPULSE_PORT", "8001"))
    base = f"http://localhost:{port}"
    if not wait_for_server(base, timeout_s=20.0):
        print(f"  [note] {base} isn't responding yet — opening anyway (the service")
        print(f"         may still be starting). If F-Pulse isn't installed as a")
        print(f"         service, start it with:  fpulse install-service")
    if getattr(args, "no_open", False):
        print(f"  --no-open: F-Pulse is reachable at {base} (window not opened).")
        return
    # Cache-bust the shell URL so a rebuilt / upgraded UI is ALWAYS loaded
    # fresh. The embedded WebView2 keeps its own HTTP cache across launches
    # and will otherwise serve a stale index.html after an update. Hashed
    # JS/CSS stay cached; only the tiny shell is re-fetched each launch.
    url = f"{base}/?_={int(_time.time())}"
    # Open the WebView2 (Edge engine) app window — chromeless, and the only
    # path where we can set a reliable DEFAULT ZOOM (80%, user preference).
    # Falls back to a chromeless Edge --app browser window if WebView2 isn't
    # available.
    if not open_native_window(url):
        open_app_window(url)


def cmd_stop(args):
    """`fpulse stop` — clean shutdown of the F-Pulse instance recorded
    at ``<cwd>/.fpulse/runtime/instance.json``.

    Applies the three-signal ownership check (PID alive + on recorded
    port + cmdline matches uvicorn-fpulse signature) and only stops
    processes that pass ALL three. Foreign processes on the same port
    are NEVER touched. If the recorded PID has already died on its
    own, the stale runtime file is removed.

    The same check shipped two turns ago in stop.ps1 — this is the
    Python sibling, sharing the same on-disk JSON format so a dev
    can run `start.ps1` then `fpulse stop` (or vice-versa) and the
    second tool finds the first tool's instance correctly.
    """
    from fpulse.cli.runtime_state import (
        read_runtime, remove_runtime, runtime_file, stop_owned_process,
    )

    instance = read_runtime()
    if instance is None:
        print(f"  No F-Pulse instance is recorded as running.")
        print(f"  (Looked for {runtime_file()})")
        return

    print(f"  Found recorded instance: {instance.instance_id}")
    print(f"  Started: {instance.started_at}")
    print(f"  Mode:    {instance.mode}")
    print(f"  Ports:   frontend={instance.frontend_port}, backend={instance.backend_port}")

    any_stopped = False
    any_skipped = False

    # Backend
    if instance.backend_pid > 0:
        ok = stop_owned_process(instance.backend_pid, instance.backend_port,
                                  kind="backend", cwd_marker=instance.cwd)
        if ok:
            print(f"  Stopped backend  (PID {instance.backend_pid}, port {instance.backend_port})")
            any_stopped = True
        else:
            print(f"  Backend PID {instance.backend_pid} no longer ours — skipping (3-signal check).")
            any_skipped = True

    # Frontend - skip if it's the same PID as backend (open mode, single process).
    if instance.frontend_pid > 0 and instance.frontend_pid != instance.backend_pid:
        ok = stop_owned_process(instance.frontend_pid, instance.frontend_port,
                                  kind="frontend", cwd_marker=instance.cwd)
        if ok:
            print(f"  Stopped frontend (PID {instance.frontend_pid}, port {instance.frontend_port})")
            any_stopped = True
        else:
            print(f"  Frontend PID {instance.frontend_pid} no longer ours — skipping (3-signal check).")
            any_skipped = True

    remove_runtime()

    if any_stopped:
        print(f"  Done. Re-run `fpulse open` anytime.")
    elif not any_skipped:
        print(f"  Nothing to stop — recorded processes had already exited. Runtime file removed.")
    else:
        print()
        print(f"  Note: PIDs were skipped because the 3-signal ownership check")
        print(f"  (PID alive + still on recorded port + cmdline matches signature)")
        print(f"  did not all pass. This is the safety mechanism that prevents")
        print(f"  accidentally killing recycled PIDs or unrelated apps.")


def cmd_run(args):
    """Execute a pipeline."""
    print(f"Executing pipeline: {args.pipeline_id}...")
    result = _api_request("POST", f"/api/execution/run/{args.pipeline_id}")
    exec_id = result.get("execution_id", result.get("id", "unknown"))
    print(f"Execution started: {exec_id}")
    print(f"Status: {result.get('status', 'submitted')}")

    if args.wait:
        print("Waiting for completion...")
        while True:
            time.sleep(2)
            status = _api_request("GET", f"/api/monitor/executions?limit=1")
            if status and isinstance(status, list):
                latest = status[0] if status else {}
                s = latest.get("status", "unknown")
                if s in ("success", "failed", "error"):
                    print(f"Finished: {s}")
                    if s != "success":
                        sys.exit(1)
                    break
                print(f"  ... {s} ({latest.get('steps_completed', 0)}/{latest.get('steps_total', 0)} steps)")


def cmd_list(args):
    """List all pipelines."""
    workflows = _api_request("GET", "/api/workflows")
    if not workflows:
        print("No pipelines found.")
        return
    _print_table(workflows, [
        ("id", "ID", 12),
        ("name", "NAME", 30),
        ("description", "DESCRIPTION", 40),
        ("version", "VER", 4),
    ])
    print(f"\n{len(workflows)} pipeline(s)")


def cmd_status(args):
    """Check execution status."""
    execs = _api_request("GET", f"/api/executions/?workflow_id={args.pipeline_id}&limit=5")
    if not execs:
        print("No executions found.")
        return
    _print_table(execs, [
        ("id", "EXEC ID", 12),
        ("status", "STATUS", 10),
        ("steps_completed", "DONE", 5),
        ("steps_total", "TOTAL", 5),
        ("started_at", "STARTED", 20),
        ("duration_ms", "DURATION", 10),
    ])


def cmd_logs(args):
    """View execution logs."""
    logs = _api_request("GET", f"/api/logs/{args.execution_id}")
    if isinstance(logs, dict):
        steps = logs.get("steps", [])
        for step in steps:
            status = step.get("status", "?")
            icon = {"success": "✓", "error": "✗", "running": "⟳", "skipped": "○"}.get(status, "?")
            print(f"  {icon} [{step.get('step_type', '?')}] {step.get('step_label', step.get('step_id', '?'))} — {status}")
            if step.get("error"):
                print(f"    Error: {step['error']}")
            if step.get("duration_ms"):
                print(f"    Duration: {step['duration_ms']}ms")
    elif isinstance(logs, list):
        for entry in logs:
            print(f"  [{entry.get('level', 'INFO')}] {entry.get('message', '')}")


def cmd_export(args):
    """Export pipeline as JSON."""
    result = _api_request("POST", f"/api/templates/export/{args.pipeline_id}")
    output = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Exported to {args.output}")
    else:
        print(output)


def cmd_import(args):
    """Import pipeline from JSON file."""
    with open(args.file, "r") as f:
        data = json.load(f)
    name = args.name or data.get("name", os.path.splitext(os.path.basename(args.file))[0])
    result = _api_request("POST", "/api/templates/import", {
        "name": name,
        "description": data.get("description", ""),
        "steps": data.get("steps", []),
        "connections": data.get("connections", []),
    })
    print(f"Imported: {result.get('id', 'unknown')} (v{result.get('version', 1)})")


def cmd_deploy(args):
    """Deploy pipeline to production."""
    result = _api_request("POST", f"/api/workflows/{args.pipeline_id}/deploy", {
        "environment": "prod",
    })
    print(f"Deployed: {args.pipeline_id}")
    print(f"Status: {result.get('status', 'deployed')}")


def cmd_health(args):
    """Check server health."""
    result = _api_request("GET", "/api/health/ready")
    status = result.get("status", "unknown")
    icon = "✓" if status == "ok" else "⚠" if status == "degraded" else "✗"
    print(f"{icon} F-Pulse {result.get('version', '?')} — {result.get('product', 'F-Pulse')}")
    print(f"  Status:    {status}")
    print(f"  Tier:      {result.get('tier', 'free')}")
    print(f"  Projects:  {result.get('projects', 0)}")
    print(f"  Nodes:     {result.get('node_types', 0)} types")
    sched = result.get("scheduler", {})
    print(f"  Scheduler: {'running' if sched.get('status') == 'ok' else 'stopped'} ({sched.get('active_jobs', 0)} active)")


def cmd_version(args):
    """Show version info."""
    print("F-Pulse v1.0.0")
    print("AI-native, human-governed data pipeline builder")
    print(f"Python {sys.version.split()[0]}")


def cmd_selftest(args):
    """Import the full server stack + heavy deps, then exit — validates that a
    packaged/frozen build is COMPLETE before anyone relies on it. Imports
    only: starts no server, opens no port, writes no data. The Windows
    installer runs this post-install, and operators can run it any time to
    confirm an install can actually boot.

    ASCII-only output on purpose — the frozen console may be cp1252.
    """
    import importlib
    mods = [
        "duckdb", "pandas", "pyarrow", "numpy", "cryptography", "bcrypt",
        "fastapi", "uvicorn", "starlette", "pydantic", "httpx",
        "openpyxl", "docx", "reportlab", "fastavro", "yaml", "psutil",
    ]
    failed = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001 - report every gap, don't abort
            failed.append((m, f"{type(e).__name__}: {e}"))
    # Building the FastAPI app is the real end-to-end check: it pulls in
    # every router, node, connector, and the DuckDB layer.
    routes = 0
    try:
        from fpulse.main import app
        routes = len(getattr(app, "routes", []))
    except Exception as e:  # noqa: BLE001
        failed.append(("fpulse.main:app", f"{type(e).__name__}: {e}"))
    if failed:
        print("SELFTEST FAILED - bundle is incomplete:")
        for m, e in failed:
            print(f"  [X] {m}: {e}")
        sys.exit(1)
    print(f"SELFTEST OK - {len(mods)} deps + fpulse.main app ({routes} routes) imported.")
    sys.exit(0)


def cmd_worker(args):
    """Launch the Stage 5 worker daemon (fpulse.worker.main).

    Thin wrapper so the operator command is the natural
    ``python -m fpulse worker`` instead of the two-module
    ``python -m fpulse.worker``. Exit code propagates.
    """
    from fpulse.worker import main as worker_main
    sys.exit(worker_main())


def cmd_seed_admin(args):
    """Reset the seeded super_admin password to 'admin' (DEV ONLY).

    Stage 1 — extracted from main.py module-import time. Previously this
    ran on EVERY backend boot via the FPULSE_DEV_SEED env var, which
    coupled DB writes to import. Now it's an explicit operator action:

        python -m fpulse seed-admin

    Use after a fresh `data/` wipe so scripts/seed-test-users.ps1 can
    authenticate as admin and create the 4 test users via the
    admin-invite API. Refuses to run unless the environment explicitly
    identifies itself as dev, or --force is passed.
    """
    # Fail CLOSED. This previously defaulted to "dev" when FPULSE_ENV was
    # unset and only refused when it contained 'prod' — so the guard never
    # fired on any install that doesn't set FPULSE_ENV, which includes the
    # shipped docker-compose.yml (it sets FPULSE_MODE=prod, not FPULSE_ENV;
    # FPULSE_ENV appears only as a commented line in .env.example). That
    # turned a dev convenience into a one-command path to a known-password
    # super_admin on a production container — the reset below writes the
    # hash directly, bypassing the password policy that would reject
    # 'admin'. Now: proceed only on an explicit dev signal.
    env = os.environ.get("FPULSE_ENV", "").strip().lower()
    mode = os.environ.get("FPULSE_MODE", "").strip().lower()
    looks_dev = env in ("dev", "development", "local", "test", "ci")
    if not looks_dev and not args.force:
        print(
            f"Refusing to seed admin: this resets admin@fpulse.local to the "
            f"known password 'admin' and bypasses the password policy, so it "
            f"only runs when the environment explicitly says dev. Got "
            f"FPULSE_ENV={env or '<unset>'!r}, FPULSE_MODE={mode or '<unset>'!r}. "
            f"Set FPULSE_ENV=dev on a throwaway install, or pass --force if "
            f"you really mean it.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Direct DB access — does NOT go through FastAPI / app_state. This is
    # the whole point of moving the seed out of main.py: the operator
    # can reset the admin password without booting the server.
    data_dir = os.environ.get(
        "FPULSE_DATA_DIR",
        os.path.join(os.getcwd(), "data"),
    )
    os.makedirs(data_dir, exist_ok=True)

    from fpulse.storage.database import Database
    from fpulse.auth.store import UserStore
    from fpulse.auth.models import User

    db = Database(os.path.join(data_dir, "fpulse.db"))
    try:
        users = UserStore(db=db)
        admin = users.get_user("admin")
        if admin is None:
            print(
                "No 'admin' user found in the database. Boot the server "
                "once first so the bootstrap admin is created, then re-run "
                "this command.",
                file=sys.stderr,
            )
            sys.exit(1)

        admin.password_hash = User.hash_password("admin")
        admin.is_active = True
        users._save_user(admin)
        print(
            "F-Pulse dev-seed: admin@fpulse.local password reset to 'admin'.\n"
            "Run scripts/seed-test-users.ps1 to create the 4 test users.",
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def cmd_reconcile_audit(args):
    """Compare dual-written table row counts between SQLite and Postgres.

    Stage 3b validation helper. Supports three tables via --table:
      audit_log (default) — first Stage 3b store
      lifecycle_events    — second Stage 3b store
      alert_logs          — third Stage 3b store

    Runs one-shot by default; pass --watch N to poll every N seconds.
    Exits 0 when counts match, 1 when they diverge (useful for CI /
    cron monitoring).

    Pass --backfill to copy SQLite rows missing from PG one-way
    (SQLite→PG). Uses the same per-table write path the live
    dual-write uses, with ON CONFLICT DO NOTHING in the SQL, so
    re-running is safe. Backfill runs before the count tick, so the
    post-backfill delta is what gets printed.

    Reads FPULSE_DATA_DIR (defaults to ./data) for SQLite and
    FPULSE_DB_URL for Postgres. If FPULSE_DB_URL is unset, PG is
    reported as 'not configured'; --backfill without PG configured
    exits 2.
    """
    import asyncio
    import json as _json
    from datetime import datetime

    # Per-table config. The sqlite side for audit_log uses explicit
    # columns (not the JSON-blob insert_json pattern); lifecycle_events
    # and alert_logs use the blob pattern with `data` holding the
    # pydantic-dumped payload. Backfill's "row → write-kwargs" closure
    # lives in each entry so the CLI code stays shape-agnostic.
    #
    # --table all: print the state of every Stage 3b store in one
    # invocation. Backfill is refused in 'all' mode — backfill is a
    # per-table operation; the operator picks one explicitly so the
    # report is unambiguous about which table was backfilled.
    table = (getattr(args, "table", None) or "audit_log").lower()

    # audit_log: the SQLite row has a `details` TEXT column holding
    # json.dumps(...) output (or NULL), and all audit fields as typed
    # columns. Backfill translates `details` back to a dict and maps
    # 1:1 to write_audit_event's kwargs.
    def _audit_sqlite_rows(db_):
        return db_.fetchall(
            "SELECT id, timestamp, user_id, user_email, action, resource_type, "
            "resource_id, details, ip_address, user_agent FROM audit_log"
        )

    def _audit_row_to_kwargs(row):
        raw = row.get("details")
        try:
            details = _json.loads(raw) if raw else {}
        except Exception:
            details = {}
        return dict(
            entry_id=row["id"],
            timestamp=row["timestamp"],
            user_id=row["user_id"] or "",
            user_email=row["user_email"] or "",
            action=row["action"] or "",
            resource_type=row["resource_type"] or "",
            resource_id=row.get("resource_id") or "",
            details=details,
            ip_address=row.get("ip_address") or "",
            user_agent=row.get("user_agent") or "",
        )

    # lifecycle_events: JSON-blob shape. The `data` column holds the
    # full LifecycleEvent payload; indexed columns hold workflow_id /
    # workspace_id / event / timestamp for query routing.
    def _lifecycle_sqlite_rows(db_):
        return db_.fetchall(
            "SELECT id, workflow_id, workspace_id, event, data, timestamp "
            "FROM lifecycle_events"
        )

    def _lifecycle_row_to_kwargs(row):
        try:
            data = _json.loads(row["data"]) if row.get("data") else {}
        except Exception:
            data = {}
        return dict(
            entry_id=row["id"],
            timestamp=row["timestamp"],
            workflow_id=row["workflow_id"] or "",
            workspace_id=row.get("workspace_id") or "default",
            event=row["event"] or "",
            data=data,
        )

    # alert_logs: JSON-blob shape. Triggered_at is the timestamp column
    # (distinct from lifecycle_events' `timestamp`). Rule_id + workflow_id
    # are indexed columns; workspace_id inherited from the parent rule.
    def _alert_log_sqlite_rows(db_):
        return db_.fetchall(
            "SELECT id, rule_id, workflow_id, workspace_id, data, triggered_at "
            "FROM alert_logs"
        )

    def _alert_log_row_to_kwargs(row):
        try:
            data = _json.loads(row["data"]) if row.get("data") else {}
        except Exception:
            data = {}
        return dict(
            entry_id=row["id"],
            triggered_at=row["triggered_at"],
            rule_id=row["rule_id"] or "",
            workflow_id=row["workflow_id"] or "",
            workspace_id=row.get("workspace_id") or "default",
            data=data,
        )

    _TABLES = {
        "audit_log": {
            "sqlite_table": "audit_log",
            "pg_init": "init_audit_schema",
            "pg_count": "count_audit_events",
            "pg_write": "write_audit_event",
            "sqlite_rows": _audit_sqlite_rows,
            "row_to_kwargs": _audit_row_to_kwargs,
        },
        "lifecycle_events": {
            "sqlite_table": "lifecycle_events",
            "pg_init": "init_lifecycle_schema",
            "pg_count": "count_lifecycle_events",
            "pg_write": "write_lifecycle_event",
            "sqlite_rows": _lifecycle_sqlite_rows,
            "row_to_kwargs": _lifecycle_row_to_kwargs,
        },
        "alert_logs": {
            "sqlite_table": "alert_logs",
            "pg_init": "init_alert_log_schema",
            "pg_count": "count_alert_log_events",
            "pg_write": "write_alert_log_event",
            "sqlite_rows": _alert_log_sqlite_rows,
            "row_to_kwargs": _alert_log_row_to_kwargs,
        },
    }
    # Resolve the table argument into a concrete list. 'all' expands to
    # every Stage 3b store in a fixed order (audit_log first since it's
    # the most-written store and operator's first check).
    if table == "all":
        tables_to_run = ["audit_log", "lifecycle_events", "alert_logs"]
    elif table in _TABLES:
        tables_to_run = [table]
    else:
        print(
            f"Unknown --table {table!r}. Valid: all, {', '.join(_TABLES.keys())}",
            file=sys.stderr,
        )
        sys.exit(2)

    data_dir = os.environ.get(
        "FPULSE_DATA_DIR",
        os.path.join(os.getcwd(), "data"),
    )
    pg_url = os.environ.get("FPULSE_DB_URL", "")

    backfill = bool(getattr(args, "backfill", False))
    if backfill and table == "all":
        print(
            "reconcile-audit --backfill is a per-table operation; run "
            "it explicitly for each store (audit_log / lifecycle_events "
            "/ alert_logs) so the report is unambiguous.",
            file=sys.stderr,
        )
        sys.exit(2)
    if backfill and not pg_url:
        print(
            "reconcile-audit --backfill requires FPULSE_DB_URL to point "
            "at Postgres. Aborting.",
            file=sys.stderr,
        )
        sys.exit(2)

    from fpulse.storage.database import Database
    db = Database(os.path.join(data_dir, "fpulse.db"))

    pg = None
    if pg_url:
        from fpulse.storage.database_pg import PostgresDatabase
        pg = PostgresDatabase(pg_url)

    # Track which PG schemas we've ensured so --table all doesn't re-run
    # init_*_schema on every tick. DDL is idempotent but there's no
    # reason to issue it repeatedly under --watch.
    _ensured_schemas: set[str] = set()

    async def _ensure_pg(cfg_):
        """Lazy init of the PG pool + the one schema we're about to
        touch. Idempotent init_<table>_schema is safe to re-run on an
        already-migrated DB, but we cache per-table to avoid repeat
        DDL under --watch."""
        if pg is None:
            return
        if not pg._initialised:
            await pg.init()
        init_name = cfg_["pg_init"]
        if init_name not in _ensured_schemas:
            await getattr(pg, init_name)()
            _ensured_schemas.add(init_name)

    async def _do_backfill(cfg_, dry_run: bool = False):
        """Copy SQLite rows missing from PG one-way. Uses per-row writes
        because (a) the dual-write path already implements them and
        (b) Stage 3b deltas are small (hundreds, not millions).
        Idempotent — the PG write uses ON CONFLICT DO NOTHING.

        When ``dry_run`` is True, skips the PG write and returns the
        count of rows that WOULD be inserted. No side effects. Useful
        as a pre-flight before a real backfill.

        Returns (attempted, inserted, failed). 'inserted' is attempted
        minus failed; actual PG row count can be checked via the tick
        that runs immediately after. In dry-run mode, failed is always
        0 and inserted equals attempted (nothing is attempted for real).
        """
        await _ensure_pg(cfg_)
        pg_ids = await pg.fetch_ids(cfg_["sqlite_table"])
        sqlite_rows = cfg_["sqlite_rows"](db)
        missing = [r for r in sqlite_rows if r["id"] not in pg_ids]
        if dry_run:
            # Nothing written. Return the count that WOULD be attempted
            # so the caller prints an informative dry-run summary.
            return (len(missing), len(missing), 0)
        write_fn = getattr(pg, cfg_["pg_write"])
        attempted = 0
        failed = 0
        for row in missing:
            attempted += 1
            try:
                await write_fn(**cfg_["row_to_kwargs"](row))
            except Exception as exc:
                failed += 1
                # Truncate very long PG errors — one per failed row at
                # info level is enough for operator triage.
                logger.info(
                    "backfill: row id=%s failed: %s",
                    row.get("id"), str(exc)[:200],
                )
        return (attempted, attempted - failed, failed)

    json_output = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run and not backfill:
        print(
            "reconcile-audit --dry-run only applies to --backfill runs. "
            "A read-only tick has no side effects to preview.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Human and JSON emitters share the same computed values; we just
    # pick where they render. JSON mode prints one line per event so the
    # output is line-delimited JSON (jq / grep friendly under --watch).
    def _emit_tick(table_name: str, sqlite_count: int, pg_count: int | None, ts: str):
        if pg_count is None:
            if json_output:
                print(_json.dumps({
                    "event": "tick",
                    "ts": ts,
                    "table": table_name,
                    "sqlite": sqlite_count,
                    "pg": None,
                    "pg_configured": False,
                    "delta": None,
                }))
            else:
                print(f"[{ts}] {table_name}: sqlite={sqlite_count}  pg=<not configured>")
            return 0
        delta = sqlite_count - pg_count
        if json_output:
            print(_json.dumps({
                "event": "tick",
                "ts": ts,
                "table": table_name,
                "sqlite": sqlite_count,
                "pg": pg_count,
                "pg_configured": True,
                "delta": delta,
                "ok": delta == 0,
            }))
        else:
            marker = "OK" if delta == 0 else f"DELTA={delta:+d}"
            print(f"[{ts}] {table_name}: sqlite={sqlite_count}  pg={pg_count}  {marker}")
        return 0 if delta == 0 else 1

    def _emit_backfill(table_name: str, attempted: int, inserted: int, failed: int, ts: str, dry: bool):
        if json_output:
            print(_json.dumps({
                "event": "backfill",
                "ts": ts,
                "table": table_name,
                "attempted": attempted,
                "inserted": inserted,
                "failed": failed,
                "dry_run": dry,
            }))
        else:
            mode = "DRY-RUN " if dry else ""
            print(
                f"[{ts}] {table_name}: {mode}backfill "
                f"attempted={attempted} inserted={inserted} failed={failed}"
            )

    async def _one_tick(cfg_):
        sqlite_table_ = cfg_["sqlite_table"]
        sqlite_count = db.count(sqlite_table_)
        pg_count = None
        if pg is not None:
            await _ensure_pg(cfg_)
            pg_count = await getattr(pg, cfg_["pg_count"])()
        ts = datetime.utcnow().strftime("%H:%M:%S")
        return _emit_tick(sqlite_table_, sqlite_count, pg_count, ts)

    async def _all_tick():
        """Run one tick per table in tables_to_run. Returns the worst
        (highest) exit code across the tables, so any divergence
        surfaces as non-zero."""
        worst = 0
        for tname in tables_to_run:
            code = await _one_tick(_TABLES[tname])
            if code > worst:
                worst = code
        return worst

    async def _run():
        try:
            if backfill:
                # backfill mode is guaranteed single-table (all is rejected
                # above), so tables_to_run has exactly one entry.
                cfg_ = _TABLES[tables_to_run[0]]
                attempted, inserted, failed = await _do_backfill(cfg_, dry_run=dry_run)
                ts = datetime.utcnow().strftime("%H:%M:%S")
                _emit_backfill(cfg_["sqlite_table"], attempted, inserted, failed, ts, dry_run)
                # After backfill, print the post-state once so the
                # operator sees the new delta without a second invocation.
                # In dry-run we still print the pre-state tick — it's
                # the same as the post-state since nothing was written.
                code = await _one_tick(cfg_)
                # If any rows failed, surface that as a non-zero exit
                # independent of the delta, so CI catches silent PG errors.
                if failed > 0:
                    return 1
                return code
            if args.watch and args.watch > 0:
                while True:
                    await _all_tick()
                    await asyncio.sleep(args.watch)
            else:
                return await _all_tick()
        finally:
            if pg is not None:
                await pg.close()

    try:
        exit_code = asyncio.run(_run())
    finally:
        try:
            db.close()
        except Exception:
            pass
    sys.exit(exit_code or 0)


# ── doctor (local runtime / port / service health) ──────────────────

def _pid_alive(pid: int) -> bool:
    """True if the given PID is a live process."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except Exception:
            return False


def _port_holder(port: int) -> int:
    """PID listening on ``port`` (loopback), or 0 if none."""
    try:
        from fpulse.cli.runtime_state import _port_holder_pid
        return _port_holder_pid(int(port))
    except Exception:
        return 0


def _probe(url: str, timeout: float = 3.0):
    """GET ``url`` and return parsed JSON (or {} on non-JSON), None if unreachable."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode()
            try:
                return json.loads(body)
            except Exception:
                return {}
    except Exception:
        return None


def _age_str(started_at: str) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(started_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 60:
            return f"{int(secs)}s"
        if secs < 3600:
            return f"{int(secs // 60)}m"
        if secs < 86400:
            return f"{int(secs // 3600)}h"
        return f"{int(secs // 86400)}d"
    except Exception:
        return "?"


def cmd_doctor(args):
    """`fpulse doctor` — diagnose local runtime/port/service health and
    optionally repair a stale runtime file.

    OSS users repeatedly hit a confusing failure: a stale
    ``.fpulse/runtime/instance.json`` points the Vite proxy at a backend
    port nothing is listening on (a previous launch died), so the UI looks
    broken even though a live backend is on the default port. This command
    surfaces that (and other local-env issues) in one place, and
    ``--repair`` clears the stale file.

    ASCII-only output on purpose — a frozen/installed console may be cp1252.
    """
    from fpulse.cli.runtime_state import read_runtime, runtime_file, remove_runtime

    def _mark(ok: bool) -> str:
        return "[OK]" if ok else "[!!]"

    print("F-Pulse doctor")
    print("=" * 56)

    inst = read_runtime()
    rf = runtime_file()
    stale = False
    all_dead = False

    if inst is None:
        print(f"runtime file : none  ({rf})")
        print("               no launcher instance recorded — fine if you run the")
        print("               always-on service or `fpulse serve` directly.")
    else:
        print(f"runtime file : {rf}")
        print(f"  instance   : {inst.instance_id}  mode={inst.mode}  age={_age_str(inst.started_at)}")
        print(f"  frontend   : port {inst.frontend_port}  pid {inst.frontend_pid}")
        print(f"  backend    : port {inst.backend_port}  pid {inst.backend_pid}")
        be_alive = _pid_alive(inst.backend_pid)
        holder = _port_holder(inst.backend_port)
        holds_port = inst.backend_pid > 0 and holder == inst.backend_pid
        print(f"  {_mark(be_alive)} backend pid alive")
        print(f"  {_mark(holds_port)} backend pid holds port {inst.backend_port}"
              + ("" if holds_port else f"  (port held by pid {holder or 'none'})"))
        all_dead = (not _pid_alive(inst.backend_pid)) and (not _pid_alive(inst.frontend_pid))
        # Stale = recorded backend isn't the live listener on its port.
        stale = (inst.backend_pid > 0 and not be_alive) or (holder not in (0, inst.backend_pid))
        if stale:
            print("  [!!] STALE — the recorded instance no longer matches the live")
            print("       process; Vite would proxy /api to a dead port.")

    # Backend health probe (recorded port, then --port/env/default).
    port = getattr(args, "port", None) or (inst.backend_port if inst else 0) \
        or int(os.environ.get("FPULSE_PORT", "8001"))
    base = f"http://127.0.0.1:{port}"
    health = _probe(base + "/api/health/ready")
    if health is not None:
        print(f"{_mark(True)} backend    : {base} responding "
              f"({health.get('status', '?')}, {health.get('node_types', '?')} node types)")
    else:
        print(f"{_mark(False)} backend    : {base}/api/health not responding")

    # Datastore directory.
    data_dir = os.environ.get("FPULSE_DATA_DIR") or os.path.join(os.getcwd(), "data")
    print(f"{_mark(os.path.isdir(data_dir))} datastore  : {data_dir}")

    # Local LLM (optional) — Ollama default endpoint.
    oll = _probe("http://127.0.0.1:11434/api/tags", timeout=2.0)
    print(f"{_mark(oll is not None)} local LLM  : Ollama 127.0.0.1:11434 "
          + ("reachable" if oll is not None else "not reachable (optional)"))

    # Repair.
    if getattr(args, "repair", False):
        print()
        if stale or all_dead:
            removed = remove_runtime()
            print(f"{_mark(removed)} repair     : "
                  + ("removed stale runtime file" if removed else "nothing to remove"))
        else:
            print(f"[..] repair     : runtime file looks healthy — nothing to repair")
    elif stale:
        print()
        print("Fix: `fpulse doctor --repair` clears the stale runtime file, then")
        print("     restart the frontend (it will use the live backend port).")


# ── CLI entry point ──────────────────────────────────────────────────

def cmd_backup(args):
    """PR 6 — Deployment hardening.

    Snapshot the entire ``FPULSE_DATA_DIR`` (SQLite DB, step-cache
    parquets, uploads, sample data) into a single .tar.gz that can be
    restored on a fresh install with ``fpulse restore``.

    Why not just ``cp``? The SQLite DB has WAL + shm sidecars; copying
    the .db while the server is running yields a partial DB. We use
    SQLite's online backup API for the DB (atomic, consistent snapshot
    while the server holds the file open) and tar+gzip the rest.

    Safe to run with the server live — Stage 3a (May 2026) added the
    online-backup path to ``Database.backup_to``.
    """
    import sqlite3
    import tarfile
    import tempfile
    from pathlib import Path
    from datetime import datetime, timezone

    data_dir = os.environ.get("FPULSE_DATA_DIR")
    if not data_dir:
        # Match _resolve_data_dir() in main.py — defaults to ./data
        data_dir = os.path.join(os.getcwd(), "data")
    data_dir = os.path.abspath(data_dir)

    if not os.path.isdir(data_dir):
        print(f"FPULSE_DATA_DIR does not exist: {data_dir}", file=sys.stderr)
        sys.exit(1)

    out = args.to
    if not out:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = f"fpulse-backup-{ts}.tar.gz"
    out_path = os.path.abspath(out)

    db_src = os.path.join(data_dir, "fpulse.db")
    with tempfile.TemporaryDirectory() as staging:
        # 1. SQLite online-backup → atomic .db copy that won't tear under WAL writes
        if os.path.exists(db_src):
            db_dst = os.path.join(staging, "fpulse.db")
            src = sqlite3.connect(db_src)
            try:
                dst = sqlite3.connect(db_dst)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            print(f"  Snapshotted DB: {db_src}", file=sys.stderr)

        # 2. tar.gz: the online-backup'd DB + everything else (uploads,
        # step-cache parquets, sample data). The original .db is replaced
        # by the snapshot when we walk data_dir below.
        snapshot_db_path = os.path.join(staging, "fpulse.db") if os.path.exists(db_src) else None

        with tarfile.open(out_path, "w:gz") as tar:
            for root, _dirs, files in os.walk(data_dir):
                for name in files:
                    if name in {"fpulse.db", "fpulse.db-wal", "fpulse.db-shm"}:
                        # Skip live DB files; we add the online-backup'd copy below.
                        continue
                    fpath = os.path.join(root, name)
                    arcname = os.path.relpath(fpath, data_dir)
                    try:
                        tar.add(fpath, arcname=arcname)
                    except (OSError, FileNotFoundError) as exc:
                        print(f"  Skipped {arcname}: {exc}", file=sys.stderr)
            if snapshot_db_path:
                tar.add(snapshot_db_path, arcname="fpulse.db")

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"Backup written to {out_path} ({size_mb:.1f} MB)")


def cmd_restore(args):
    """PR 6 — counterpart to ``backup``.

    Extract a backup tarball into a target directory. Refuses to clobber
    a non-empty directory unless ``--force`` is set, because restoring on
    top of a live install would corrupt the running database. Typical
    flow:
      1. Stop the server (Docker: ``docker compose down``)
      2. ``fpulse restore --from backup.tar.gz --to /data``
      3. Start the server back up — it'll pick up the restored DB.
    """
    import tarfile

    src = args.src
    if not os.path.exists(src):
        print(f"Backup file not found: {src}", file=sys.stderr)
        sys.exit(1)

    dest = os.path.abspath(args.to or os.environ.get("FPULSE_DATA_DIR") or "./data")
    if os.path.isdir(dest) and os.listdir(dest) and not args.force:
        print(
            f"Refusing to restore into non-empty directory: {dest}\n"
            f"  Pass --force to overwrite, or pick an empty path with --to.",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(dest, exist_ok=True)
    with tarfile.open(src, "r:gz") as tar:
        # Built-in safety against tar-bomb / path-traversal — refuse any
        # entry whose resolved path escapes the destination.
        for member in tar.getmembers():
            resolved = os.path.realpath(os.path.join(dest, member.name))
            if not resolved.startswith(os.path.realpath(dest) + os.sep) and resolved != os.path.realpath(dest):
                print(f"Refused unsafe path in archive: {member.name}", file=sys.stderr)
                sys.exit(2)
        tar.extractall(dest)

    print(f"Restored {src} → {dest}")
    print("Start the F-Pulse server pointing FPULSE_DATA_DIR at this directory.")


def main():
    parser = argparse.ArgumentParser(
        prog="fpulse",
        description="F-Pulse — AI-native data pipeline builder CLI",
    )
    parser.add_argument("--url", default=None, help="F-Pulse server URL (default: http://localhost:8001)")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the F-Pulse server")
    p_serve.add_argument("--port", type=int, default=None, help="Port (default: 8001)")
    p_serve.add_argument(
        "--host", default=None,
        help="Bind host (default: 127.0.0.1 loopback-only). "
             "Pass 0.0.0.0 to expose on the LAN — see "
             "docs/install/security-hardening.md for the risk tradeoff.",
    )
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    p_serve.add_argument(
        "--open", action="store_true",
        help="After starting, auto-open the default browser to the local URL. "
             "Falls back to a printed URL in WSL/Docker/SSH/headless-Linux. "
             "Also enables port-fallback (8001→8002→... if the default is in use).",
    )
    p_serve.add_argument(
        "--no-open", action="store_true",
        help="With --open, skip the browser launch but keep port-fallback. "
             "For CI runs and headless environments that want the friendlier "
             "port behaviour without the auto-open attempt.",
    )

    # open — alias for `serve --open`, the 1.0 "one-command launch" verb
    p_open = subparsers.add_parser(
        "open",
        help="Start F-Pulse and open it in your default browser (one command).",
    )
    p_open.add_argument("--port", type=int, default=None,
                        help="Port (default: 8001; auto-falls back if in use)")
    p_open.add_argument("--host", default=None,
                        help="Bind host (default: 127.0.0.1 loopback)")
    p_open.add_argument("--no-open", action="store_true",
                        help="Start the backend but don't try to open the browser")

    # app — open the UI in a chromeless app-mode window WITHOUT starting a
    # server (the installed service already serves it). This is the verb the
    # Start-Menu / desktop shortcut targets.
    p_app = subparsers.add_parser(
        "app",
        help="Open F-Pulse in an app-mode window (does not start a server)",
    )
    p_app.add_argument("--port", type=int, default=None,
                       help="Port the running service is on (default: 8001)")
    p_app.add_argument("--no-open", action="store_true",
                       help="Don't actually open a window (diagnostic)")

    # stop — clean shutdown via the runtime ownership file
    subparsers.add_parser(
        "stop",
        help=(
            "Stop the F-Pulse instance recorded at .fpulse/runtime/"
            "instance.json. Applies a 3-signal ownership check (PID "
            "alive + on recorded port + cmdline matches uvicorn-fpulse "
            "signature) and only stops processes that pass all three. "
            "Foreign processes on the same port are NEVER touched."
        ),
    )

    # run
    p_run = subparsers.add_parser("run", help="Execute a pipeline")
    p_run.add_argument("pipeline_id", help="Pipeline ID to execute")
    p_run.add_argument("--wait", "-w", action="store_true", help="Wait for completion")

    # list
    subparsers.add_parser("list", aliases=["ls"], help="List all pipelines")

    # status
    p_status = subparsers.add_parser("status", help="Check pipeline execution status")
    p_status.add_argument("pipeline_id", help="Pipeline ID")

    # logs
    p_logs = subparsers.add_parser("logs", help="View execution logs")
    p_logs.add_argument("execution_id", help="Execution ID")

    # export
    p_export = subparsers.add_parser("export", help="Export pipeline as JSON")
    p_export.add_argument("pipeline_id", help="Pipeline ID to export")
    p_export.add_argument("-o", "--output", help="Output file path")

    # import
    p_import = subparsers.add_parser("import", help="Import pipeline from JSON")
    p_import.add_argument("file", help="JSON file to import")
    p_import.add_argument("--name", help="Override pipeline name")

    # deploy
    p_deploy = subparsers.add_parser("deploy", help="Deploy pipeline to production")
    p_deploy.add_argument("pipeline_id", help="Pipeline ID to deploy")

    # health
    subparsers.add_parser("health", help="Check server health")

    # version
    subparsers.add_parser("version", help="Show version info")

    # selftest — validate a packaged/frozen build can import the full stack
    subparsers.add_parser(
        "selftest",
        help="Verify this build can import the full server stack (starts no server)",
    )

    # doctor — local runtime/port/service diagnostics + stale-runtime repair
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Diagnose local runtime/ports/service/datastore/LLM health; "
             "--repair clears a stale runtime file",
    )
    p_doctor.add_argument(
        "--repair", action="store_true",
        help="Remove a stale .fpulse/runtime/instance.json so the launcher / "
             "Vite stop trusting a dead instance",
    )
    p_doctor.add_argument(
        "--port", type=int, default=None,
        help="Backend port to health-probe (default: recorded port / FPULSE_PORT / 8001)",
    )

    # backup / restore (PR 6 — deployment hardening, May 17 2026)
    p_backup = subparsers.add_parser(
        "backup",
        help="Snapshot FPULSE_DATA_DIR into a single .tar.gz",
    )
    p_backup.add_argument(
        "--to", help="Output path (default: ./fpulse-backup-<TS>.tar.gz)",
    )
    p_restore = subparsers.add_parser(
        "restore",
        help="Extract a backup tarball into a target directory",
    )
    p_restore.add_argument("src", help="Path to the .tar.gz produced by `fpulse backup`")
    p_restore.add_argument(
        "--to", help="Target dir (default: $FPULSE_DATA_DIR or ./data)",
    )
    p_restore.add_argument(
        "--force", action="store_true",
        help="Allow restoring on top of a non-empty directory (DESTRUCTIVE)",
    )

    # seed-admin (Stage 1 — moved out of main.py module-import time)
    p_seed = subparsers.add_parser(
        "seed-admin",
        help="Reset seeded super_admin password to 'admin' (DEV ONLY)",
    )
    p_seed.add_argument(
        "--force", action="store_true",
        help="Run even when FPULSE_ENV looks like production",
    )

    # worker (Stage 5 Phase 2 — out-of-process execution daemon)
    subparsers.add_parser(
        "worker",
        help=(
            "Run the Stage 5 worker daemon. Polls the shared Redis "
            "queue (FPULSE_REDIS_URL) and executes jobs out-of-process. "
            "Replaces the in-process worker pool in split-container "
            "Plus deployments."
        ),
    )

    # reconcile-audit (Stage 3b dual-write validation; --table selects store)
    p_recon = subparsers.add_parser(
        "reconcile-audit",
        help="Compare dual-written table counts between SQLite and Postgres",
    )
    p_recon.add_argument(
        "--table", default="all",
        choices=["all", "audit_log", "lifecycle_events", "alert_logs"],
        help=(
            "Which dual-written table to reconcile. 'all' prints every "
            "Stage 3b store in one invocation (default). Pass a single "
            "table name when using --backfill."
        ),
    )
    p_recon.add_argument(
        "--watch", type=int, default=0, metavar="SECONDS",
        help="Poll every N seconds (0 = one-shot)",
    )
    p_recon.add_argument(
        "--backfill", action="store_true",
        help=(
            "Copy SQLite rows missing from Postgres one-way (SQLite→PG), "
            "then print the post-state. Idempotent via ON CONFLICT DO "
            "NOTHING. Requires FPULSE_DB_URL. Exits non-zero if any row "
            "failed to write."
        ),
    )
    p_recon.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Only meaningful with --backfill. Prints how many rows WOULD "
            "be inserted without actually writing to Postgres. Useful as "
            "a pre-flight check."
        ),
    )
    p_recon.add_argument(
        "--json", action="store_true",
        help=(
            "Emit line-delimited JSON instead of the human string. Each "
            "tick is one JSON object; --backfill emits one backfill "
            "object followed by one tick object. Suitable for jq / cron "
            "monitoring / Grafana scraping."
        ),
    )

    # ── install-service / uninstall-service / service-status (2026-05-30) ──
    # Cross-platform service supervisor. Detects OS + writes the right
    # Scheduled Task (Windows) / LaunchAgent (macOS) / systemd user-unit
    # (Linux) so F-Pulse runs in the background without a terminal
    # window and auto-restarts on crash. See fpulse/cli/install_service.py.
    p_inst = subparsers.add_parser(
        "install-service",
        help="Register F-Pulse as an OS-native background service (any OS)",
    )
    p_inst.add_argument(
        "--data-dir",
        help="Override FPULSE_DATA_DIR for the service (default: per-OS sane choice)",
    )
    p_inst.add_argument(
        "--port", type=int, default=8001,
        help="Bind port for the supervised uvicorn process (default 8001)",
    )
    p_inst.add_argument(
        "--at-boot", action="store_true",
        help="Windows: run at system boot as SYSTEM (no login required). "
             "Requires an elevated (Administrator) terminal. Default is "
             "start-at-logon in your user session.",
    )

    subparsers.add_parser(
        "uninstall-service",
        help="Remove the OS-native service registration (no data loss)",
    )

    subparsers.add_parser(
        "service-status",
        help="Show whether the supervised F-Pulse service is running",
    )

    args = parser.parse_args()

    if args.url:
        os.environ["FPULSE_URL"] = args.url

    # Lazy import to avoid loading the per-OS service helpers on every
    # `fpulse --help` invocation.
    from fpulse.cli.install_service import (
        cmd_install as cmd_install_service,
        cmd_uninstall as cmd_uninstall_service,
        cmd_status as cmd_service_status,
    )

    commands = {
        "serve": cmd_serve,
        "open": cmd_open,  # 2026-06-02: 1.0 one-command launch verb
        "app": cmd_app,    # 2026-06-18: open app-mode window (no server start)
        "stop": cmd_stop,  # 2026-06-07: ownership-checked clean shutdown
        "run": cmd_run,
        "list": cmd_list,
        "ls": cmd_list,
        "status": cmd_status,
        "logs": cmd_logs,
        "export": cmd_export,
        "import": cmd_import,
        "deploy": cmd_deploy,
        "health": cmd_health,
        "version": cmd_version,
        "selftest": cmd_selftest,
        "doctor": cmd_doctor,  # 2026-06-18: local runtime/port diagnostics + repair
        "seed-admin": cmd_seed_admin,
        "reconcile-audit": cmd_reconcile_audit,
        "worker": cmd_worker,
        "backup": cmd_backup,
        "restore": cmd_restore,
        # 2026-05-30 — cross-platform service supervisor
        "install-service": cmd_install_service,
        "uninstall-service": cmd_uninstall_service,
        "service-status": cmd_service_status,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
