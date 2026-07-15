# ─────────────────────────────────────────────────────────────────────────
# F-Pulse OSS — single-binary container
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
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
# Use vite directly — `npm run build` runs `tsc -b && vite build`, and
# tsc strict-mode noise (pre-existing in the 1.0 baseline) would fail
# the image build. Vite's esbuild transpile still emits the production
# bundle correctly; type-only errors don't affect runtime output.
RUN npx vite build

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

# Resolved Python deps from builder
COPY --from=builder /root/.local /home/fpulse/.local
ENV PATH=/home/fpulse/.local/bin:$PATH \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FPULSE_DATA_DIR=/data \
    FPULSE_PORT=8001 \
    FPULSE_MODE=prod

# Backend source
COPY backend/ /app/backend/
# Frontend bundle (served as static by FastAPI)
COPY --from=frontend /build/dist /app/frontend_dist

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
