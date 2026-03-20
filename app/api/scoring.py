"""
Fraud scoring API endpoints.
Pre-auth and post-auth transaction scoring with sub-200ms SLA.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request

from app.core.security import generate_request_id, strip_pii
from app.models.schemas import (
    ScoreTransactionRequest,
    ScoreTransactionResponse,
)
from ml.inference.scoring_engine import get_scoring_engine

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/scoring", tags=["scoring"])


@router.post("/score", response_model=ScoreTransactionResponse)
async def score_transaction(body: ScoreTransactionRequest, request: Request) -> ScoreTransactionResponse:
    """
    Score a transaction for fraud risk.
    Returns fraud score, decision (approve/review/decline), and feature contributions.

    Target latency: < 200ms p99.
    """
    request_id = generate_request_id()

    logger.info(
        "api.score.request",
        request_id=request_id,
        txn_id=body.transaction.transaction_id,
        merchant_id=body.transaction.merchant_id,
        amount_cents=body.transaction.amount_cents,
    )

    engine = get_scoring_engine()
    result = await engine.score_transaction(body.transaction)

    if not body.include_feature_contributions:
        result = result.model_copy(update={"feature_contributions": {}})

    return ScoreTransactionResponse(score=result, request_id=request_id)


@router.get("/health")
async def scoring_health() -> dict:
    """Check if the scoring engine is ready."""
    engine = get_scoring_engine()
    return {
        "ready": engine.is_ready,
        "active_version": engine._active_version,
    }
