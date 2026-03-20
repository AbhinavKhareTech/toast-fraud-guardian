"""
Webhook ingestion and dispute management API.
Handles chargeback notifications from payment processors and
exposes dispute lifecycle operations.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.observability import WEBHOOK_RECEIVED
from app.core.security import generate_request_id
from app.integrations.payments import get_adapter
from app.models.schemas import (
    ChargebackEvent,
    DisputeStatus,
    DisputeSubmitResponse,
)
from app.services.dispute_service import DisputeService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/disputes", tags=["disputes"])


@router.post("/webhooks/{processor}")
async def receive_webhook(
    processor: str,
    request: Request,
) -> dict[str, str]:
    """
    Ingest chargeback webhook from a payment processor.
    Verifies signature, parses event, and queues dispute workflow.
    """
    if processor not in ("stripe", "square", "toast"):
        raise HTTPException(status_code=400, detail=f"Unknown processor: {processor}")

    body = await request.body()
    headers = dict(request.headers)

    # Verify webhook signature
    adapter = get_adapter(processor)
    if not await adapter.verify_webhook(body, headers):
        logger.warning("webhook.signature_failed", processor=processor)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    WEBHOOK_RECEIVED.labels(provider=processor, event_type="chargeback").inc()

    # Parse payload
    import orjson
    payload = orjson.loads(body)
    payload_hash = hashlib.sha256(body).hexdigest()

    # Extract chargeback event (processor-specific parsing in adapter)
    txn = await adapter.parse_chargeback_webhook(payload)
    if txn is None:
        # Not a chargeback event, acknowledge and ignore
        return {"status": "ignored", "reason": "not_a_chargeback_event"}

    # Build ChargebackEvent
    chargeback_id = payload.get("data", {}).get("object", {}).get("id", generate_request_id())
    reason_code = payload.get("data", {}).get("object", {}).get("reason", "unknown")

    chargeback = ChargebackEvent(
        chargeback_id=chargeback_id,
        transaction_id=txn.transaction_id,
        merchant_id=txn.merchant_id,
        card_token=txn.card_token,
        amount_cents=txn.amount_cents,
        currency=txn.currency,
        reason_code=reason_code,
        deadline=datetime(2099, 12, 31, tzinfo=timezone.utc),  # TODO: parse from payload
        received_at=datetime.now(timezone.utc),
        payment_processor=processor,
        raw_payload_hash=payload_hash,
    )

    # Create dispute and queue workflow
    trace_id = generate_request_id()
    async with get_db_session() as session:
        svc = DisputeService(session)
        record = await svc.create_dispute(chargeback, trace_id=trace_id)

    # Queue agent workflow asynchronously
    from workers.tasks import process_dispute_task
    process_dispute_task.delay(
        dispute_id=record.id,
        chargeback_data=chargeback.model_dump(mode="json"),
    )

    logger.info(
        "webhook.chargeback.processed",
        processor=processor,
        dispute_id=record.id,
        merchant_id=txn.merchant_id,
    )

    return {"status": "accepted", "dispute_id": record.id}


@router.get("/{dispute_id}")
async def get_dispute(dispute_id: str) -> dict[str, Any]:
    """Fetch a dispute by ID."""
    async with get_db_session() as session:
        svc = DisputeService(session)
        record = await svc.get_dispute(dispute_id)

    if record is None:
        raise HTTPException(status_code=404, detail="Dispute not found")

    return {
        "id": record.id,
        "chargeback_id": record.chargeback_id,
        "transaction_id": record.transaction_id,
        "merchant_id": record.merchant_id,
        "amount_cents": record.amount_cents,
        "reason_code": record.reason_code,
        "status": record.status,
        "fraud_score": record.fraud_score,
        "evidence_strength": record.evidence_strength,
        "decision": record.decision,
        "decision_rationale": record.decision_rationale,
        "deadline": record.deadline.isoformat() if record.deadline else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "outcome": record.outcome,
    }


@router.get("/")
async def list_disputes(
    status: str | None = Query(None),
    merchant_id: str | None = Query(None),
    limit: int = Query(50, le=200),
) -> dict[str, Any]:
    """List disputes with optional filtering."""
    from sqlalchemy import select
    from app.models.orm import DisputeRecord

    async with get_db_session() as session:
        query = select(DisputeRecord).order_by(DisputeRecord.created_at.desc()).limit(limit)
        if status:
            query = query.where(DisputeRecord.status == status)
        if merchant_id:
            query = query.where(DisputeRecord.merchant_id == merchant_id)

        result = await session.execute(query)
        records = result.scalars().all()

    return {
        "disputes": [
            {
                "id": r.id,
                "merchant_id": r.merchant_id,
                "amount_cents": r.amount_cents,
                "status": r.status,
                "decision": r.decision,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
        "count": len(records),
    }


@router.post("/{dispute_id}/review")
async def submit_review_decision(
    dispute_id: str,
    decision: str = Body(..., embed=True),
    reviewer_id: str = Body(..., embed=True),
    notes: str = Body("", embed=True),
) -> DisputeSubmitResponse:
    """
    Human reviewer submits a decision for a dispute in the review queue.
    Valid decisions: 'approve' (submit dispute), 'reject' (abandon dispute).
    """
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Decision must be 'approve' or 'reject'")

    trace_id = generate_request_id()
    async with get_db_session() as session:
        svc = DisputeService(session)
        record = await svc.get_dispute(dispute_id)

        if record is None:
            raise HTTPException(status_code=404, detail="Dispute not found")
        if record.status != DisputeStatus.PENDING_REVIEW.value:
            raise HTTPException(status_code=409, detail=f"Dispute not in review state (current: {record.status})")

        if decision == "approve":
            await svc.set_decision(
                dispute_id,
                decision="auto_submit",
                rationale=f"Human review approved by {reviewer_id}: {notes}",
                trace_id=trace_id,
                actor=f"human:{reviewer_id}",
            )
            # Queue submission
            from workers.tasks import submit_dispute_task
            submit_dispute_task.delay(dispute_id=dispute_id)

            return DisputeSubmitResponse(
                dispute_id=dispute_id,
                status=DisputeStatus.AUTO_SUBMITTED,
                message="Dispute approved and queued for submission.",
            )
        else:
            await svc.set_decision(
                dispute_id,
                decision="decline_dispute",
                rationale=f"Human review rejected by {reviewer_id}: {notes}",
                trace_id=trace_id,
                actor=f"human:{reviewer_id}",
            )
            return DisputeSubmitResponse(
                dispute_id=dispute_id,
                status=DisputeStatus.EXPIRED,
                message="Dispute declined by reviewer.",
            )


@router.get("/metrics/summary")
async def dispute_metrics() -> dict[str, Any]:
    """Get dispute automation and win rate metrics."""
    async with get_db_session() as session:
        svc = DisputeService(session)
        return await svc.get_metrics()
