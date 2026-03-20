"""
SQLAlchemy ORM models for dispute cases, audit log, and feedback loop.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DisputeRecord(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chargeback_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    transaction_id: Mapped[str] = mapped_column(String(128), index=True)
    merchant_id: Mapped[str] = mapped_column(String(128), index=True)
    card_token: Mapped[str] = mapped_column(String(128))
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    reason_code: Mapped[str] = mapped_column(String(32))
    reason_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    fraud_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_strength: Mapped[str | None] = mapped_column(String(16), nullable=True)
    evidence_items: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dispute_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_processor: Mapped[str] = mapped_column(String(32))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    human_reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_disputes_status_deadline", "status", "deadline"),
        Index("ix_disputes_merchant_created", "merchant_id", "created_at"),
    )


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    request_id: Mapped[str] = mapped_column(String(128))
    merchant_id: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    __table_args__ = (
        Index("ix_audit_trace_action", "trace_id", "action"),
    )


class FraudScoreLog(Base):
    """Immutable log of every fraud score computed. Feeds the training feedback loop."""

    __tablename__ = "fraud_score_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(String(128), index=True)
    merchant_id: Mapped[str] = mapped_column(String(128), index=True)
    card_token: Mapped[str] = mapped_column(String(128))
    fraud_score: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(16))
    model_version: Mapped[str] = mapped_column(String(32))
    feature_vector: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    feature_contributions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Feedback: updated post-resolution
    actual_fraud: Mapped[bool | None] = mapped_column(default=None, nullable=True)
    feedback_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_score_log_merchant_scored", "merchant_id", "scored_at"),
    )


class ModelVersion(Base):
    """Track deployed model versions for reproducibility."""

    __tablename__ = "model_versions"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_path: Mapped[str] = mapped_column(String(512))
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=False)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    training_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
