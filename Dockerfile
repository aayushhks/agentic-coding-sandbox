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

# Bake a read-only SQLite database seeded from the committed results, so the image is
# self-contained: `docker run` shows the real v1/v2 runs with no external database. Override
# DATABASE_URL (compose / RDS / Neon) to point at Postgres instead.
COPY docs/results/groq-llama-3.3-70b-v1.json docs/results/groq-llama-3.3-70b-v2.json /app/docs/results/
ENV DATABASE_URL="sqlite+aiosqlite:////app/backend/eval.db"
RUN python -m app.eval.import_results --results /app/docs/results/groq-llama-3.3-70b-v1.json --create-tables \
    && python -m app.eval.import_results --results /app/docs/results/groq-llama-3.3-70b-v2.json

# the compiled SPA — FastAPI mounts it at / (main.py resolves ../frontend/dist)
COPY --from=frontend /frontend/dist /app/frontend/dist

EXPOSE 8000
# honor an injected $PORT (Railway etc.), defaulting to 8000 for local/compose use
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8000\")}/health').status == 200 else 1)"]

# the image serves the API + dashboard; migrations are run by the orchestrator
# (docker-compose.yml / railway.json) since they depend on a reachable database
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
