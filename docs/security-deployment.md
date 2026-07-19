# F-Pulse OSS — Secure Deployment Guide

**Audience:** operators putting F-Pulse on a network reachable by anyone
who is not them. If F-Pulse only listens on `127.0.0.1` for your own
laptop, you can skip this document — the defaults are safe.

This covers the things F-Pulse cannot enforce alone: TLS termination,
header forwarding, the security-relevant environment variables, master
key file permissions, and CSP overrides for iframe embedding.

For install, upgrade, backup, and disaster recovery see
[`deployment.md`](deployment.md). That is the operator runbook; this is
the hardening checklist.

If you only have time for one section, read **§3 Security-relevant
environment variables** and **§4 File-system permissions**.

---

## 1. Network topology

F-Pulse listens on plain HTTP. Always front it with a reverse proxy that
terminates TLS:

```
Browser ─HTTPS─▶ nginx / Caddy / Traefik ─HTTP─▶ uvicorn (F-Pulse) :8001
```

Direct internet exposure of the uvicorn port is not supported. F-Pulse
only emits `Strict-Transport-Security` when the request arrived over TLS
(directly, or via `X-Forwarded-Proto: https` from a reverse proxy) — so
without a proxy you also lose HSTS.

## 2. Reverse-proxy snippets

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name fpulse.example.com;

    ssl_certificate     /etc/letsencrypt/live/fpulse.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/fpulse.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;

    # Pipeline file-upload page
    client_max_body_size 100M;

    location / {
        proxy_pass         http://127.0.0.1:8001;
        proxy_http_version 1.1;

        # Headers F-Pulse reads to detect TLS and emit HSTS
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;

        # WebSocket for live execution updates
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_read_timeout 1h;
    }
}

# Redirect plain HTTP
server {
    listen 80;
    server_name fpulse.example.com;
    return 301 https://$server_name$request_uri;
}
```

### Caddy

```caddy
fpulse.example.com {
    reverse_proxy 127.0.0.1:8001 {
        header_up X-Forwarded-Proto https
        header_up X-Real-IP        {remote}
    }
    request_body {
        max_size 100MB
    }
}
```

Caddy auto-provisions Let's Encrypt; nothing else to configure for TLS.

## 3. Security-relevant environment variables

These are the variables F-Pulse OSS actually reads. Anything not listed
here is read by F-Pulse+ and does not apply to OSS.

| Variable | Purpose | Default |
|---|---|---|
| `FPULSE_DATA_DIR` | Where SQLite + master key + run history live | `./data` |
| `FPULSE_MASTER_KEY_FILE` | Override path for the symmetric credential-encryption key | `<FPULSE_DATA_DIR>/secret.key` |
| `FPULSE_CORS_ORIGINS` | Comma-separated allowed origins (`https://app.example.com,https://admin.example.com`) | dev: localhost regex; prod: must be set |
| `FPULSE_CSP` | Full Content-Security-Policy string override; empty value disables CSP entirely | conservative baseline (see §6) |
| `FPULSE_HSTS_MAX_AGE` | HSTS `max-age` in seconds — only emitted over TLS | `31536000` (1 year) |
| `FPULSE_MAX_CONCURRENT_RUNS` | Cap on simultaneously-executing pipelines | host-CPU heuristic |
| `FPULSE_DISABLE_SECURITY_HEADERS=1` | Bypass the security-headers middleware — **debugging only, never in production** | unset |

`FPULSE_CORS_ORIGINS` matters: with `allow_credentials=True`, browsers
refuse the `*` wildcard, which is why F-Pulse falls back to a localhost
regex in dev and **requires you to set an explicit allowlist in
production**. If you do not set it and clients live anywhere other than
localhost, login will silently fail.

## 4. File-system permissions

The data directory must be owned by the F-Pulse process user with no
world or group access. The master key file (resolved per
[`security/encryptor.py`](../backend/fpulse/security/encryptor.py)) is
the symmetric key that decrypts every stored connection credential.

**On POSIX**, F-Pulse refuses to start if the master key file is group-
or world-readable (`mode & 0o077 != 0`). The error tells you the offending
mode and how to fix it.

```bash
# Recommended layout (POSIX)
sudo useradd --system --home /var/lib/fpulse --shell /usr/sbin/nologin fpulse
sudo install -d -m 0700 -o fpulse -g fpulse /var/lib/fpulse
sudo install -d -m 0700 -o fpulse -g fpulse /var/lib/fpulse/backups
```

**On Windows**, F-Pulse does not enforce the perm check — NTFS ACLs are
the operator's responsibility. Place the data directory under the
service-account profile (`%LOCALAPPDATA%\fpulse` for a logged-in user,
or under the service account's profile for a service install); NTFS
defaults there are owner-only.

The master key cannot be regenerated without losing access to every
stored credential. Back it up the moment you create it (see
[`deployment.md`](deployment.md) §4).

## 5. Header forwarding (for HSTS)

F-Pulse only emits `Strict-Transport-Security` when it sees the request
arrived over TLS. It checks, in order:

1. `scope.scheme == "https"` — direct exposure, unusual.
2. `X-Forwarded-Proto: https` — proxy mode, the common case.

If your reverse proxy does not set `X-Forwarded-Proto`, F-Pulse will not
send HSTS and browsers will allow first-visit downgrades. The nginx and
Caddy snippets in §2 set this header correctly; copy them verbatim if
you are unsure.

## 6. Content-Security-Policy

F-Pulse ships a conservative default CSP suitable for the bundled React
UI. The default sets `frame-ancestors 'none'`, which blocks any site
(including your own) from embedding F-Pulse in an iframe.

If you need to embed F-Pulse — for example, behind a corporate portal —
override the full CSP via `FPULSE_CSP`:

```env
FPULSE_CSP=default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'self' https://portal.example.com; base-uri 'self'; form-action 'self'
```

Setting `FPULSE_CSP=` (empty) disables the header entirely. Do not do
this unless a downstream proxy is injecting a stricter policy of its
own.

## 7. Rate limiting

F-Pulse does not ship an application-layer rate limiter in v1. For any
deployment reachable from the public internet, put rate limiting at the
proxy layer:

- **nginx**: `limit_req_zone` + `limit_req`
- **Caddy**: `rate_limit` plugin
- **Cloudflare / AWS WAF / GCP Armor**: per-IP rules for `/api/auth/*`
  and `/api/execute/*` at minimum

A starting point is 60 req/min per IP overall, with a tighter 10 req/min
on any login or execution endpoint.

## 8. What F-Pulse already does for you

The security-headers middleware
([`backend/fpulse/api/security_headers.py`](../backend/fpulse/api/security_headers.py))
sets the following on every response, so you do **not** need to add them
at the proxy layer (though it does no harm if you do):

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), …`
- `Content-Security-Policy: …` (see §6)
- `Cross-Origin-Opener-Policy: same-origin`
- `X-XSS-Protection: 0` (modern guidance: rely on CSP)
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` —
  only over TLS
- `Server: fpulse` (the original uvicorn/Starlette identifier is
  stripped; `X-Powered-By` is dropped entirely)

A clean Nessus / OWASP ZAP / Mozilla Observatory scan of an F-Pulse OSS
install behind a TLS proxy should report no missing security-header
findings out of the box.

## 9. See also

- [Deployment guide](deployment.md) — install, three-component upgrade
  flow, backup, disaster recovery
- [Scaling guide](scaling.md) — capacity planning
- `security.md` at the repository root — vulnerability disclosure policy
- [Trust posture](TRUST.md) — what F-Pulse sends to LLM providers, and
  what it never sends
