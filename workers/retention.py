"""
Data retention and GDPR/CCPA compliance worker.

Handles:
- Periodic PII cleanup based on configurable retention windows
- Right-to-delete requests for specific merchants or card tokens
- Audit log rotation (separate from PII retention)

Runs as a Celery beat scheduled task.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import delete, select, text, update

from app.core.config import get_settings
from app.core.database import get_db_session
from app.models.orm import AuditLogEntry, DisputeRecord, FraudScoreLog
from workers.tasks import celery_app

logger = structlog.get_logger(__name__)


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="workers.retention.enforce_pii_retention")
def enforce_pii_retention() -> dict[str, Any]:
    """
    Delete PII-bearing records older than the configured retention window.
    Runs daily via Celery beat.

    Affected tables:
    - fraud_score_log: card_token, feature_vector (contains derived PII)
    - disputes: card_token, dispute_letter (may contain PII in evidence text)

    Audit log entries are retained separately (longer window, no PII fields).
    """
    return _run_async(_enforce_pii_retention_async())


async def _enforce_pii_retention_async() -> dict[str, Any]:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.pii_retention_days)
    results: dict[str, Any] = {"cutoff_date": cutoff.isoformat(), "actions": []}

    logger.info("retention.pii.started", cutoff=cutoff.isoformat(), retention_days=settings.pii_retention_days)

    async with get_db_session() as session:
        # 1. Scrub PII from old fraud score logs
        #    We keep the record for model performance tracking but null out PII
        score_result = await session.execute(
            update(FraudScoreLog)
            .where(FraudScoreLog.scored_at < cutoff)
            .where(FraudScoreLog.card_token != "[expired]")
            .values(
                card_token="[expired]",
                feature_vector=None,
            )
        )
        score_scrubbed = score_result.rowcount  # type: ignore[union-attr]
        results["actions"].append({"table": "fraud_score_log", "scrubbed": score_scrubbed})

        # 2. Scrub PII from old resolved disputes
        #    Keep dispute shell for metrics but remove card token and letter content
        dispute_result = await session.execute(
            update(DisputeRecord)
            .where(DisputeRecord.created_at < cutoff)
            .where(DisputeRecord.card_token != "[expired]")
            .where(DisputeRecord.outcome.isnot(None))  # Only scrub resolved disputes
            .values(
                card_token="[expired]",
                dispute_letter="[redacted per retention policy]",
                evidence_items=None,
            )
        )
        dispute_scrubbed = dispute_result.rowcount  # type: ignore[union-attr]
        results["actions"].append({"table": "disputes", "scrubbed": dispute_scrubbed})

    logger.info("retention.pii.completed", **results)
    return results


@celery_app.task(name="workers.retention.enforce_audit_log_retention")
def enforce_audit_log_retention() -> dict[str, Any]:
    """
    Delete audit log entries beyond the configured retention window.
    Default: 7 years for financial compliance.
    Runs weekly.
    """
    return _run_async(_enforce_audit_retention_async())


async def _enforce_audit_retention_async() -> dict[str, Any]:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.audit_log_retention_days)

    async with get_db_session() as session:
        result = await session.execute(
            delete(AuditLogEntry).where(AuditLogEntry.timestamp < cutoff)
        )
        deleted = result.rowcount  # type: ignore[union-attr]

    logger.info("retention.audit.completed", cutoff=cutoff.isoformat(), deleted=deleted)
    return {"cutoff_date": cutoff.isoformat(), "deleted": deleted}


@celery_app.task(name="workers.retention.process_deletion_request")
def process_deletion_request(
    entity_type: str,
    entity_id: str,
    requested_by: str,
) -> dict[str, Any]:
    """
    Process a GDPR/CCPA right-to-delete request.
    Removes all PII associated with a merchant or card token.

    Args:
        entity_type: "merchant" or "card_token"
        entity_id: The merchant_id or card_token to delete
        requested_by: Audit trail for who requested deletion
    """
    if entity_type not in ("merchant", "card_token"):
        return {"error": f"Invalid entity_type: {entity_type}"}

    return _run_async(_process_deletion_async(entity_type, entity_id, requested_by))


async def _process_deletion_async(
    entity_type: str,
    entity_id: str,
    requested_by: str,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enable_gdpr_deletion:
        return {"error": "GDPR deletion is disabled in configuration"}

    logger.info(
        "retention.deletion_request",
        entity_type=entity_type,
        entity_id=entity_id[:8] + "...",
        requested_by=requested_by,
    )

    results: dict[str, int] = {}

    async with get_db_session() as session:
        if entity_type == "merchant":
            # Scrub all dispute records for this merchant
            r1 = await session.execute(
                update(DisputeRecord)
                .where(DisputeRecord.merchant_id == entity_id)
                .values(
                    card_token="[deleted]",
                    dispute_letter="[deleted per GDPR/CCPA request]",
                    evidence_items=None,
                    decision_rationale="[deleted]",
                )
            )
            results["disputes_scrubbed"] = r1.rowcount  # type: ignore[union-attr]

            r2 = await session.execute(
                update(FraudScoreLog)
                .where(FraudScoreLog.merchant_id == entity_id)
                .values(card_token="[deleted]", feature_vector=None, feature_contributions=None)
            )
            results["score_logs_scrubbed"] = r2.rowcount  # type: ignore[union-attr]

        elif entity_type == "card_token":
            r1 = await session.execute(
                update(DisputeRecord)
                .where(DisputeRecord.card_token == entity_id)
                .values(
                    card_token="[deleted]",
                    dispute_letter="[deleted per GDPR/CCPA request]",
                    evidence_items=None,
                )
            )
            results["disputes_scrubbed"] = r1.rowcount  # type: ignore[union-attr]

            r2 = await session.execute(
                update(FraudScoreLog)
                .where(FraudScoreLog.card_token == entity_id)
                .values(card_token="[deleted]", feature_vector=None)
            )
            results["score_logs_scrubbed"] = r2.rowcount  # type: ignore[union-attr]

        # Also purge from Redis feature store
        try:
            from app.core.redis_client import get_feature_store, get_sequence_cache
            import hashlib

            if entity_type == "card_token":
                token_hash = hashlib.sha256(entity_id.encode()).hexdigest()[:16]
                store = get_feature_store()
                cache = get_sequence_cache()
                await store.delete(f"card_profile:{token_hash}")
                await cache.delete(f"card_seq:{token_hash}")
                results["redis_keys_deleted"] = 2
            elif entity_type == "merchant":
                store = get_feature_store()
                await store.delete(f"merchant_profile:{entity_id}")
                results["redis_keys_deleted"] = 1
        except Exception as e:
            logger.warning("retention.redis_cleanup_error", error=str(e))
            results["redis_error"] = str(e)

    logger.info("retention.deletion_completed", entity_type=entity_type, **results)
    return {"entity_type": entity_type, "status": "completed", **results}
