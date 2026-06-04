# Multi-stage build: compile the React dashboard, then ship a Python image that
# serves both the API and the built SPA from a single FastAPI process.

# ---- stage 1: build the dashboard ----
FROM node:22-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
# -> /frontend/dist

# ---- stage 2: backend runtime (also serves the built dashboard) ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/backend \
    PATH=/app/backend/.venv/bin:$PATH

WORKDIR /app/backend

# install only production dependencies first, as a cached layer
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

# application source
COPY backend/ ./

# the compiled SPA — FastAPI mounts it at / (main.py resolves ../frontend/dist)
COPY --from=frontend /frontend/dist /app/frontend/dist

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status == 200 else 1)"]

# the image serves the API + dashboard; migrations are run by the orchestrator
# (see docker-compose.yml) since they depend on a reachable database
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
