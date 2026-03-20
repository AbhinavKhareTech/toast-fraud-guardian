"""
Monitoring tasks for operational health.
Checks model drift, approaching deadlines, and captures metrics snapshots.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.observability import ACTIVE_REVIEW_QUEUE, DISPUTES_AUTOMATION_RATE
from workers.tasks import celery_app

logger = structlog.get_logger(__name__)


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="workers.monitoring.check_model_health")
def check_model_health() -> dict[str, Any]:
    """
    Verify the fraud scoring model is loaded and responsive.
    Logs warning if model is in fallback/heuristic mode.
    """
    try:
        from ml.inference.scoring_engine import get_scoring_engine

        engine = get_scoring_engine()
        health: dict[str, Any] = {
            "is_ready": engine.is_ready,
            "active_version": engine._active_version,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not engine.is_ready:
            logger.warning("monitoring.model_not_ready", **health)
        else:
            logger.info("monitoring.model_healthy", **health)

        return health

    except Exception as e:
        logger.error("monitoring.model_check_failed", error=str(e))
        return {"error": str(e), "is_ready": False}


@celery_app.task(name="workers.monitoring.check_dispute_deadlines")
def check_dispute_deadlines() -> dict[str, Any]:
    """
    Alert on disputes approaching their submission deadline.
    Disputes within 48 hours of deadline are flagged as urgent.
    """
    return _run_async(_check_deadlines_async())


async def _check_deadlines_async() -> dict[str, Any]:
    from sqlalchemy import select, func
    from app.core.database import get_db_session
    from app.models.orm import DisputeRecord

    now = datetime.now(timezone.utc)
    urgent_cutoff = now + timedelta(hours=48)
    warning_cutoff = now + timedelta(days=7)

    async with get_db_session() as session:
        # Urgent: < 48 hours remaining, still pending
        urgent = await session.execute(
            select(func.count())
            .select_from(DisputeRecord)
            .where(DisputeRecord.status.in_(["received", "scoring", "evidence_collection", "pending_review"]))
            .where(DisputeRecord.deadline <= urgent_cutoff)
            .where(DisputeRecord.deadline > now)
        )
        urgent_count = urgent.scalar() or 0

        # Warning: < 7 days remaining
        warning = await session.execute(
            select(func.count())
            .select_from(DisputeRecord)
            .where(DisputeRecord.status.in_(["received", "scoring", "evidence_collection", "pending_review"]))
            .where(DisputeRecord.deadline <= warning_cutoff)
            .where(DisputeRecord.deadline > urgent_cutoff)
        )
        warning_count = warning.scalar() or 0

        # Expired: past deadline and unresolved
        expired = await session.execute(
            select(func.count())
            .select_from(DisputeRecord)
            .where(DisputeRecord.status.in_(["received", "scoring", "evidence_collection", "pending_review"]))
            .where(DisputeRecord.deadline <= now)
        )
        expired_count = expired.scalar() or 0

    result = {
        "urgent_48h": urgent_count,
        "warning_7d": warning_count,
        "expired": expired_count,
        "checked_at": now.isoformat(),
    }

    if urgent_count > 0:
        logger.warning("monitoring.urgent_deadlines", **result)
    elif expired_count > 0:
        logger.error("monitoring.expired_disputes", **result)
    else:
        logger.info("monitoring.deadlines_ok", **result)

    return result


@celery_app.task(name="workers.monitoring.snapshot_dispute_metrics")
def snapshot_dispute_metrics() -> dict[str, Any]:
    """
    Capture current dispute metrics and update Prometheus gauges.
    Provides the automation rate and review queue size.
    """
    return _run_async(_snapshot_metrics_async())


async def _snapshot_metrics_async() -> dict[str, Any]:
    from app.core.database import get_db_session
    from app.services.dispute_service import DisputeService

    async with get_db_session() as session:
        svc = DisputeService(session)
        metrics = await svc.get_metrics()

        # Update queue size
        queue = await svc.get_review_queue(limit=1000)
        ACTIVE_REVIEW_QUEUE.set(len(queue))

    logger.info("monitoring.metrics_snapshot", **metrics)
    return metrics
