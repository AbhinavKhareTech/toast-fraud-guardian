# Multi-stage Dockerfile for toast-fraud-guardian
# Stage 1: Base with dependencies
# Stage 2: Production image (slim)

# --- Stage 1: Builder ---
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install ".[dev]"

# --- Stage 2: Production ---
FROM python:3.11-slim AS production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser -d /app appuser

COPY --from=builder /install /usr/local

COPY app/ ./app/
COPY ml/ ./ml/
COPY workers/ ./workers/
COPY pyproject.toml .

# Create necessary directories
RUN mkdir -p /app/ml/export /app/data /app/logs \
    && chown -R appuser:appuser /app

USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Default: run API server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--loop", "uvloop"]
