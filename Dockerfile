# ─────────────────────────────────────────────────────────────────────────
# F-Pulse OSS — container
# ─────────────────────────────────────────────────────────────────────────
# One container running everything: API + scheduler + worker pool + DuckDB.
# Frontend is served via Vite dev server in development; production builds
# serve the React bundle from the FastAPI app's static mount.
#
# Multi-stage build keeps the runtime image lean by leaving the build
# toolchain (gcc, libpq-dev, npm) and pip cache out of the shipping image.
#
# For the Plus tier multi-container topology (api + worker + postgres),
# see the F-Pulse+ deployment guide.
# ─────────────────────────────────────────────────────────────────────────

# ── Stage 1: build the frontend bundle ──
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Same build the CI + release paths run: `tsc -b && vite build` (+ the CSS
# guard). Type-checking is NOT bypassed here — the image build fails on a type
# error exactly like CI, so Docker can't ship a bundle the other paths reject.
RUN npm run build

# ── Stage 2: build Python wheels ──
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt \
    && pip install --no-cache-dir --user psutil

# ── Stage 3: runtime ──
FROM python:3.11-slim AS runtime

# Non-root user — F-Pulse never needs root.
RUN groupadd -r fpulse && useradd -r -g fpulse -d /app -s /sbin/nologin fpulse

WORKDIR /app

# Resolved Python deps from builder. They were `pip install --user`ed into
# /root/.local (builder runs as root) and copied here. The runtime user `fpulse`
# has HOME=/app (useradd -d /app), so Python's default user-site would be
# /app/.local — the WRONG place. PYTHONUSERBASE points Python's user-site at the
# real location so `python -m uvicorn` (and every dep) actually resolves;
# without it the container starts and instantly dies with "No module named
# uvicorn" while the image still builds clean.
COPY --from=builder /root/.local /home/fpulse/.local
ENV PATH=/home/fpulse/.local/bin:$PATH \
    PYTHONUSERBASE=/home/fpulse/.local \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FPULSE_DATA_DIR=/data \
    FPULSE_PORT=8001 \
    FPULSE_MODE=prod

# Backend source
COPY backend/ /app/backend/
# Frontend bundle (served as static by FastAPI). Must land on the packaged
# path main.py's _resolve_frontend_dist() checks — <pkg>/frontend_dist — or the
# API boots healthy while serving a blank UI (/ 404s, healthcheck still passes).
COPY --from=frontend /build/dist /app/backend/fpulse/frontend_dist

# Persistent data dir
RUN mkdir -p /data && chown -R fpulse:fpulse /app /data /home/fpulse

USER fpulse

EXPOSE 8001

# Liveness probe — uses the lifespan-aware /api/health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:8001/api/health', timeout=3).status == 200 else 1)"

# Single uvicorn worker — F-Pulse OSS is single-node by design. The
# in-process scheduler and worker pool serialize correctly with one
# uvicorn process; multiple workers would duplicate them.
CMD ["python", "-m", "uvicorn", "fpulse.main:app", "--host", "0.0.0.0", "--port", "8001"]
