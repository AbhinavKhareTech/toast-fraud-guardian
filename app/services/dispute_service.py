"""
Dispute service: core business logic for the dispute lifecycle.
Handles creation, evidence collection, decision-making, and submission.
All operations are audited and idempotent.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import orjson
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.observability import (
    ACTIVE_REVIEW_QUEUE,
    DISPUTES_AUTOMATION_RATE,
    DISPUTES_CREATED,
    DISPUTES_SUBMITTED,
)
from app.core.security import AuditContext, strip_pii
from app.models.orm import AuditLogEntry, DisputeRecord, FraudScoreLog
from app.models.schemas import (
    ChargebackEvent,
    DisputeCase,
    DisputeStatus,
    EvidenceItem,
    EvidenceStrength,
    FraudScoreResult,
)

logger = structlog.get_logger(__name__)


class DisputeService:
    """
    Manages the dispute lifecycle from chargeback receipt to resolution.
    All state transitions are persisted and audited.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._settings = get_settings()

    async def create_dispute(
        self,
        chargeback: ChargebackEvent,
        trace_id: str,
    ) -> DisputeRecord:
        """
        Create a new dispute case from an incoming chargeback event.
        Idempotent: returns existing dispute if chargeback_id already exists.
        """
        # Check for existing dispute (idempotency)
        existing = await self._session.execute(
            select(DisputeRecord).where(
                DisputeRecord.chargeback_id == chargeback.chargeback_id
            )
        )
        if record := existing.scalar_one_or_none():
            logger.info("dispute.already_exists", dispute_id=record.id, chargeback_id=chargeback.chargeback_id)
            return record

        dispute_id = f"disp_{secrets.token_urlsafe(16)}"

        record = DisputeRecord(
            id=dispute_id,
            chargeback_id=chargeback.chargeback_id,
            transaction_id=chargeback.transaction_id,
            merchant_id=chargeback.merchant_id,
            card_token=chargeback.card_token,
            amount_cents=chargeback.amount_cents,
            currency=chargeback.currency,
            reason_code=chargeback.reason_code,
            reason_description=chargeback.reason_description,
            status=DisputeStatus.RECEIVED.value,
            payment_processor=chargeback.payment_processor,
            deadline=chargeback.deadline,
        )

        self._session.add(record)

        # Audit log
        await self._log_audit(
            trace_id=trace_id,
            merchant_id=chargeback.merchant_id,
            actor="system",
            action="dispute.created",
            resource_type="dispute",
            resource_id=dispute_id,
            details={"chargeback_id": chargeback.chargeback_id, "amount_cents": chargeback.amount_cents},
        )

        DISPUTES_CREATED.labels(source="webhook").inc()
        logger.info("dispute.created", dispute_id=dispute_id, merchant_id=chargeback.merchant_id)

        return record

    async def update_fraud_score(
        self,
        dispute_id: str,
        score_result: FraudScoreResult,
        trace_id: str,
    ) -> None:
        """Attach fraud score to a dispute and log for feedback loop."""
        await self._session.execute(
            update(DisputeRecord)
            .where(DisputeRecord.id == dispute_id)
            .values(
                fraud_score=score_result.fraud_score,
                status=DisputeStatus.SCORING.value,
            )
        )

        # Log score for training feedback
        score_log = FraudScoreLog(
            transaction_id=score_result.transaction_id,
            merchant_id=score_result.merchant_id,
            card_token="[redacted]",
            fraud_score=score_result.fraud_score,
            decision=score_result.decision.value,
            model_version=score_result.model_version,
            feature_contributions=score_result.feature_contributions,
            latency_ms=score_result.latency_ms,
        )
        self._session.add(score_log)

        await self._log_audit(
            trace_id=trace_id,
            merchant_id=score_result.merchant_id,
            actor="agent:transaction_scorer",
            action="dispute.scored",
            resource_type="dispute",
            resource_id=dispute_id,
            details={"fraud_score": score_result.fraud_score, "decision": score_result.decision.value},
        )

    async def update_evidence(
        self,
        dispute_id: str,
        evidence_items: list[EvidenceItem],
        evidence_strength: EvidenceStrength,
        trace_id: str,
    ) -> None:
        """Attach collected evidence to a dispute."""
        serialized = [item.model_dump(mode="json") for item in evidence_items]

        await self._session.execute(
            update(DisputeRecord)
            .where(DisputeRecord.id == dispute_id)
            .values(
                evidence_items=serialized,
                evidence_strength=evidence_strength.value,
                status=DisputeStatus.EVIDENCE_COLLECTION.value,
            )
        )

        await self._log_audit(
            trace_id=trace_id,
            merchant_id="",
            actor="agent:evidence_collector",
            action="dispute.evidence_updated",
            resource_type="dispute",
            resource_id=dispute_id,
            details={"evidence_count": len(evidence_items), "strength": evidence_strength.value},
        )

    async def update_dispute_letter(
        self,
        dispute_id: str,
        letter: str,
        trace_id: str,
    ) -> None:
        """Attach LLM-generated dispute letter."""
        await self._session.execute(
            update(DisputeRecord)
            .where(DisputeRecord.id == dispute_id)
            .values(
                dispute_letter=letter,
                status=DisputeStatus.EVIDENCE_WRITING.value,
            )
        )

        await self._log_audit(
            trace_id=trace_id,
            merchant_id="",
            actor="agent:evidence_writer",
            action="dispute.letter_generated",
            resource_type="dispute",
            resource_id=dispute_id,
            details={"letter_length": len(letter)},
        )

    async def set_decision(
        self,
        dispute_id: str,
        decision: str,
        rationale: str,
        trace_id: str,
        actor: str = "agent:decision_engine",
    ) -> None:
        """Set the dispute decision (auto_submit, human_review, decline_dispute)."""
        new_status = {
            "auto_submit": DisputeStatus.AUTO_SUBMITTED.value,
            "human_review": DisputeStatus.PENDING_REVIEW.value,
            "decline_dispute": DisputeStatus.EXPIRED.value,
        }.get(decision, DisputeStatus.PENDING_REVIEW.value)

        values: dict[str, Any] = {
            "decision": decision,
            "decision_rationale": rationale,
            "status": new_status,
        }
        if decision == "auto_submit":
            values["submitted_at"] = datetime.now(timezone.utc)
            DISPUTES_SUBMITTED.labels(outcome="pending").inc()

        await self._session.execute(
            update(DisputeRecord).where(DisputeRecord.id == dispute_id).values(**values)
        )

        if decision == "human_review":
            ACTIVE_REVIEW_QUEUE.inc()

        await self._log_audit(
            trace_id=trace_id,
            merchant_id="",
            actor=actor,
            action=f"dispute.decision.{decision}",
            resource_type="dispute",
            resource_id=dispute_id,
            details={"decision": decision, "rationale": rationale[:200]},
        )

    async def resolve_dispute(
        self,
        dispute_id: str,
        outcome: str,
        trace_id: str,
    ) -> None:
        """Record final dispute resolution (won/lost)."""
        status = DisputeStatus.WON.value if outcome == "won" else DisputeStatus.LOST.value
        await self._session.execute(
            update(DisputeRecord)
            .where(DisputeRecord.id == dispute_id)
            .values(
                outcome=outcome,
                status=status,
                resolved_at=datetime.now(timezone.utc),
            )
        )

        DISPUTES_SUBMITTED.labels(outcome=outcome).inc()

        await self._log_audit(
            trace_id=trace_id,
            merchant_id="",
            actor="system",
            action=f"dispute.resolved.{outcome}",
            resource_type="dispute",
            resource_id=dispute_id,
        )

    async def get_review_queue(
        self,
        limit: int = 50,
        merchant_id: str | None = None,
    ) -> list[DisputeRecord]:
        """Fetch disputes pending human review, ordered by deadline urgency."""
        query = (
            select(DisputeRecord)
            .where(DisputeRecord.status == DisputeStatus.PENDING_REVIEW.value)
            .order_by(DisputeRecord.deadline.asc())
            .limit(limit)
        )
        if merchant_id:
            query = query.where(DisputeRecord.merchant_id == merchant_id)

        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        result = await self._session.execute(
            select(DisputeRecord).where(DisputeRecord.id == dispute_id)
        )
        return result.scalar_one_or_none()

    async def get_metrics(self) -> dict[str, Any]:
        """Compute automation rate and win rate for monitoring."""
        from sqlalchemy import func

        total = await self._session.execute(
            select(func.count()).select_from(DisputeRecord)
        )
        total_count = total.scalar() or 0

        auto_submitted = await self._session.execute(
            select(func.count())
            .select_from(DisputeRecord)
            .where(DisputeRecord.decision == "auto_submit")
        )
        auto_count = auto_submitted.scalar() or 0

        won = await self._session.execute(
            select(func.count())
            .select_from(DisputeRecord)
            .where(DisputeRecord.outcome == "won")
        )
        won_count = won.scalar() or 0

        resolved = await self._session.execute(
            select(func.count())
            .select_from(DisputeRecord)
            .where(DisputeRecord.outcome.isnot(None))
        )
        resolved_count = resolved.scalar() or 0

        automation_rate = (auto_count / total_count * 100) if total_count > 0 else 0
        win_rate = (won_count / resolved_count * 100) if resolved_count > 0 else 0

        DISPUTES_AUTOMATION_RATE.set(automation_rate)

        return {
            "total_disputes": total_count,
            "auto_submitted": auto_count,
            "automation_rate_pct": round(automation_rate, 2),
            "resolved": resolved_count,
            "won": won_count,
            "win_rate_pct": round(win_rate, 2),
        }

    async def _log_audit(
        self,
        trace_id: str,
        merchant_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        ctx = AuditContext.create(
            trace_id=trace_id,
            merchant_id=merchant_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=strip_pii(details) if details else {},
        )
        entry = AuditLogEntry(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            merchant_id=ctx.merchant_id,
            actor=ctx.actor,
            action=ctx.action,
            resource_type=ctx.resource_type,
            resource_id=ctx.resource_id,
            details=ctx.metadata,
        )
        self._session.add(entry)
