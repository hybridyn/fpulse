# F-Pulse OSS — security hardening (local-first defaults)

F-Pulse OSS is a single-user, local-first tool. As of 2026-06-02 it
defaults to a hardened local-only mode that closes the LAN-exposure
class of attack. This page documents what changed, why, and how to
override the defaults when you genuinely need to.

## TL;DR

| | Before 2026-06-02 | After |
|---|---|---|
| Backend bind host | `0.0.0.0` (LAN-visible) | **`127.0.0.1`** (loopback only) |
| Cross-origin requests | Open to any origin with valid CORS preflight | Refused unless Origin/Referer is loopback |
| Dev-mode auth bypass | Works from any IP | Refused unless caller is loopback |
| UI awareness of bind state | None | Warning banner if non-loopback |
| `/api/health/bind-info` | Did not exist | Returns current bind state for the UI |

If your workflow is "open browser, type `http://localhost:8001`, build
a pipeline on your laptop" — **nothing changes**. The defaults match
that intent.

If your workflow is "deploy to a Linux VM, multiple people hit the
same instance from the LAN" — **you now have to opt in explicitly**.
That's intentional: most users were getting LAN exposure by accident.

## How to opt into LAN binding

Pick one of:

| Env var | Effect |
|---|---|
| `FPULSE_BIND_HOST=0.0.0.0` | Explicitly name the bind host |
| `FPULSE_ALLOW_LAN=1` | Convenience flag → 0.0.0.0 |
| `--host 0.0.0.0` on `fpulse serve` | CLI override (highest precedence) |

Windows examples:

```powershell
# PowerShell
$env:FPULSE_ALLOW_LAN = "1"
.\start.ps1

# cmd.exe
set FPULSE_ALLOW_LAN=1
start.bat
```

Linux / macOS:

```bash
FPULSE_ALLOW_LAN=1 fpulse serve
# or
fpulse serve --host 0.0.0.0
```

When LAN-bound, the launcher prints a `[WARNING]` line at startup
and the UI shows a sticky red banner reading "F-Pulse is exposed on
your local network." That banner is dismissible per session — there's
no way to disable it permanently. Visible-by-default exposure is the
whole point.

## Security mode: `local` vs `server` (2026-07)

The 2026-06 loopback default (above) stops *accidental* LAN exposure.
The 2026-07 `FPULSE_SECURITY_MODE` switch governs what the API does
once it *is* reachable — most importantly, whether an unauthenticated
caller is allowed at all.

| | `local` (default) | `server` |
|---|---|---|
| Who it's for | single-user laptop, loopback | exposed / multi-user self-host |
| Anonymous request | allowed → falls back to the `default` workspace | **rejected with 401** |
| One-time execution codes (`FPULSE_REQUIRE_EXECUTION_CODE`) | off | **on** |
| CORS | localhost wildcard (dev convenience) | same-origin only unless `FPULSE_CORS_ORIGINS` is set |
| AI actions that *execute* (run / cancel / test) | allowed | require a write role |

Set it explicitly:

```bash
export FPULSE_SECURITY_MODE=server      # Linux/macOS
```
```powershell
$env:FPULSE_SECURITY_MODE = "server"    # Windows
```

**`FPULSE_ALLOW_LAN=1` implies `server`.** If you opt into LAN binding
with the convenience flag, security mode defaults to `server`
automatically — you don't get an instance that's exposed *and*
anonymous by accident.

### Exposure guard — a LAN bind in `local` mode is refused

Because `local` mode allows anonymous access, binding it to a
non-loopback address would expose uploads, backfills, AI actions and
your data to anyone who can reach the port. The launcher now **refuses
to start** in that combination:

```
[SECURITY] Refusing to start: bind host '0.0.0.0' is network-reachable
but security mode is 'local', which allows ANONYMOUS access (no login).
```

To run on a network, do one of:
- `FPULSE_ALLOW_LAN=1` (implies server mode), or
- set `FPULSE_SECURITY_MODE=server` explicitly, then bind non-loopback, or
- keep it private with `--host 127.0.0.1`.

A raw `--host 0.0.0.0` (or `FPULSE_BIND_HOST=0.0.0.0`) with **no** server
mode is the one combination that's blocked — by design. This supersedes
the bare "name the bind host" rows in the opt-in table above: naming a
non-loopback host is necessary but no longer sufficient on its own.

## Why loopback isn't enough on its own — three additional defenses

### 1. Host header allowlist + Origin / Referer pinning (DNS-rebinding defense)

A loopback bind makes the API invisible to the LAN. It does NOT make
the API invisible to the user's own browser. A malicious page on the
public internet can pull a DNS-rebinding trick: register an
attacker-controlled domain, point its A record at `127.0.0.1` via
short-TTL DNS manipulation, and have the user's browser submit
requests to the local API while the browser believes it's same-origin
with the attacker's page.

The `LocalOriginGuardMiddleware` in `backend/fpulse/api/local_hardening.py`
applies **two layered defenses** when the backend is loopback-bound:

- **Primary: Host header allowlist** — every request whose `Host`
  header isn't one of `localhost`, `127.0.0.1`, `[::1]`, `::1` is
  refused with 403. This is the stronger control: the attacker
  cannot forge a loopback Host from a rebinding-controlled domain
  because browsers send the Host the user typed (or that the page
  requested). Recommended as the primary defense by GitHub Security
  (2025), NCC Group's Singularity DNS-rebinding tooling, and the MCP
  Security advisory series.

- **Secondary: Origin / Referer pinning** — additionally refuses
  cross-origin requests whose `Origin` or `Referer` points outside
  loopback. Catches CSRF-style attacks even when the Host happens
  to be valid loopback (e.g. an attacker page that loaded our HTML
  and then issued requests with the right Host).

Same-origin and no-origin requests (curl tests, server-to-self)
continue to work. Health / metrics endpoints are exempted so uptime
probes don't break.

### 2. Loopback-only auth-bypass guard

Any developer convenience that skips authentication (env-var
toggles, debug headers, etc.) MUST call
`assert_dev_auth_local_only(request)` from `local_hardening.py`
before honouring the bypass. If the request didn't arrive via
loopback, the guard returns 403 — even if the env var is set.

This stops the "I set dev mode on my laptop, then deployed the
same env to a server" foot-gun.

### 3. UI awareness — the sticky warning banner

The frontend polls `/api/health/bind-info` on first render. If
`loopback_only: false`, a sticky banner appears at the top of every
page reading:

> F-Pulse is exposed on your local network — anyone on this WiFi
> can hit the API. Set `FPULSE_BIND_HOST=127.0.0.1` to fix.

Operators who deliberately turned on LAN binding (on-prem multi-user
installs) can dismiss it for the session, but the banner returns on
reload. The intent is "you should never accidentally have LAN
exposure on without knowing it."

## When you actually want LAN binding

Three legitimate scenarios:

1. **Docker / container deploy** — the container has its own network
   namespace, so binding to `0.0.0.0` inside the container is what
   you want. The host-level network boundary is provided by Docker.
   The shipped `Dockerfile` already binds `0.0.0.0` — that's correct
   for containers.

2. **On-prem multi-user OSS** — small team, shared box, everyone hits
   `http://team-server:8001`. Set `FPULSE_ALLOW_LAN=1` on the host,
   put the box behind a firewall, and ensure auth is configured
   (don't use any dev-mode bypass).

3. **F-Pulse+ server install** — Plus is designed for the
   browser/server model. Set `FPULSE_BIND_HOST=0.0.0.0` (or run
   behind a reverse proxy that terminates TLS) and let the Plus
   license + RBAC enforce who can do what.

For scenarios #2 and #3, also consider:
- Putting F-Pulse behind a TLS-terminating reverse proxy (nginx, Caddy)
- Setting `FPULSE_BIND_HOST=127.0.0.1` and letting the proxy be the
  only network-facing process — same external behaviour, smaller
  attack surface

## Verifying the hardening is on

```bash
# When the backend is up:
curl http://127.0.0.1:8001/api/health/bind-info
```

Expected response in default OSS mode:

```json
{
  "bind_host": "127.0.0.1",
  "loopback_only": true,
  "allow_lan_flag": false,
  "warning": null
}
```

If `loopback_only` is `false` and you didn't intend LAN exposure,
unset the relevant env var and restart.

## Container / cloud deploys are not affected

These deployments intentionally bind to `0.0.0.0` because the
container's own networking is the boundary:

- `Dockerfile` (CMD line)
- `railway.toml` (startCommand)
- `.github/workflows/e2e-playwright.yml` (CI)

If you're shipping a Docker image, nothing to do. The hardening
defaults apply only to the OSS local-launcher path (`start.bat`,
`start.ps1`, `fpulse serve` without `--host`, direct `python -m
fpulse.main` without `FPULSE_BIND_HOST`).

## What's coming next — desktop packaging

The 1.0 hardening above closes the LAN-exposure risk, but the
"open browser, type localhost" UX is still developer-shaped, not
end-user-shaped. A native desktop experience is on the OSS 1.1
roadmap — see [`docs/roadmap/oss-1-1.md`](../roadmap/oss-1-1.md)
for the two candidate paths under evaluation (Tauri shell + Python
sidecar, vs. a lighter pip-install + auto-launched browser pattern
like Jupyter / Streamlit).

The desktop path is **deliberately deferred until after 1.0 launch**
so we can let real OSS usage tell us whether browser-UX friction is
the actual adoption blocker. If it is, the desktop work gets
prioritized in 1.1; if it isn't, the engineering budget goes to the
correctness items higher up on the roadmap.
