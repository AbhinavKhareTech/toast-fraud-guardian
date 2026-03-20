"""
FastAPI application entry point.
Async-first with structured logging, rate limiting, and health checks.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api import api_router
from app.core.config import get_settings
from app.core.database import close_db, init_db
from app.core.logging import setup_logging
from app.core.observability import setup_otel_tracing
from app.core.redis_client import close_all_pools
from app.core.security import generate_request_id
from app.models.schemas import HealthResponse

logger = structlog.get_logger(__name__)

_start_time: float = 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown lifecycle."""
    global _start_time
    _start_time = time.monotonic()

    setup_logging()
    setup_otel_tracing()

    settings = get_settings()
    logger.info(
        "app.starting",
        env=settings.app_env.value,
        model_version=settings.model_version,
    )

    # Initialize database
    await init_db()

    # Pre-load scoring model
    try:
        from ml.inference.scoring_engine import get_scoring_engine
        engine = get_scoring_engine()
        logger.info("app.model_loaded", ready=engine.is_ready)
    except Exception as e:
        logger.warning("app.model_load_failed", error=str(e))

    yield

    # Cleanup
    await close_db()
    await close_all_pools()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Toast Fraud Guardian",
        description="Real-time fraud detection and autonomous chargeback dispute system",
        version="0.1.0",
        lifespan=lifespan,
        debug=settings.app_debug,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request ID + timing middleware
    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", generate_request_id())
        start = time.monotonic()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response: Response = await call_next(request)

        elapsed_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"

        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            latency_ms=round(elapsed_ms, 2),
        )

        return response

    # Routes
    app.include_router(api_router)

    # Health endpoints
    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="healthy",
            version="0.1.0",
            model_version=settings.model_version,
            uptime_seconds=round(time.monotonic() - _start_time, 2),
        )

    @app.get("/ready", tags=["system"])
    async def readiness() -> dict:
        """Readiness check: verifies DB and Redis connectivity."""
        checks: dict[str, str] = {}
        try:
            from app.core.database import get_engine
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"error: {e}"

        try:
            from app.core.redis_client import get_general_redis
            r = get_general_redis()
            await r.ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"

        all_ok = all(v == "ok" for v in checks.values())
        return {"ready": all_ok, "checks": checks}

    @app.get("/metrics", tags=["system"])
    async def metrics() -> Response:
        """Prometheus metrics endpoint."""
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


# Import text for readiness query
from sqlalchemy import text

app = create_app()
