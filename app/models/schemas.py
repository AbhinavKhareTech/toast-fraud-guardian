"""
Domain models (Pydantic v2) for the fraud detection and dispute pipeline.
All models enforce PII-safe patterns: no PAN fields, token-based references only.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Enums ---

class TransactionType(str, Enum):
    CARD_PRESENT = "card_present"
    CARD_NOT_PRESENT = "card_not_present"
    CONTACTLESS = "contactless"
    MOBILE_WALLET = "mobile_wallet"
    ONLINE = "online"


class FraudDecision(str, Enum):
    APPROVE = "approve"
    REVIEW = "review"
    DECLINE = "decline"


class DisputeStatus(str, Enum):
    RECEIVED = "received"
    SCORING = "scoring"
    EVIDENCE_COLLECTION = "evidence_collection"
    EVIDENCE_WRITING = "evidence_writing"
    PENDING_REVIEW = "pending_review"
    AUTO_SUBMITTED = "auto_submitted"
    MANUALLY_SUBMITTED = "manually_submitted"
    WON = "won"
    LOST = "lost"
    EXPIRED = "expired"


class EvidenceStrength(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class ChargebackReasonCode(str, Enum):
    """Common Visa/MC reason codes relevant to restaurant fraud."""
    FRAUD_NO_AUTHORIZATION = "10.4"   # Visa
    FRAUD_CARD_ABSENT = "10.5"        # Visa
    NOT_RECOGNIZED = "13.1"           # Visa
    CANCELLED_RECURRING = "13.2"      # Visa
    NOT_AS_DESCRIBED = "13.3"         # Visa
    COUNTERFEIT = "4837"              # MC
    NO_AUTHORIZATION = "4808"         # MC
    CARDHOLDER_DISPUTE = "4853"       # MC


# --- Transaction Models ---

class DeviceSignals(BaseModel):
    """Device and network signals for fraud scoring. No PII stored."""

    model_config = ConfigDict(frozen=True)

    ip_address_hash: str = Field(description="SHA-256 hash of IP, never raw IP")
    device_fingerprint: str | None = None
    user_agent_hash: str | None = None
    is_known_proxy: bool = False
    is_tor_exit: bool = False
    geo_country: str | None = None
    geo_city: str | None = None
    geo_lat: float | None = None
    geo_lng: float | None = None


class OrderMetadata(BaseModel):
    """Restaurant-specific order context for behavioral analysis."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    item_count: int = 0
    avg_item_price_cents: int = 0
    has_alcohol: bool = False
    has_tip: bool = False
    tip_percentage: float = 0.0
    order_channel: str = "in_store"  # in_store, online, app, phone
    time_to_complete_seconds: int | None = None


class TransactionEvent(BaseModel):
    """
    Inbound transaction for fraud scoring.
    CRITICAL: card_token only. No PAN/CVV ever enters this model.
    """

    model_config = ConfigDict(frozen=True)

    transaction_id: str
    merchant_id: str
    card_token: str = Field(description="Tokenized card reference from payment processor")
    amount_cents: int = Field(gt=0)
    currency: str = "USD"
    transaction_type: TransactionType
    timestamp: datetime
    device_signals: DeviceSignals | None = None
    order_metadata: OrderMetadata | None = None
    payment_processor: str = "stripe"  # stripe, square, toast

    @field_validator("card_token")
    @classmethod
    def validate_card_token(cls, v: str) -> str:
        if not v.startswith("tok_") and not v.startswith("card_"):
            raise ValueError("card_token must be a processor token, not raw card data")
        return v


class FraudScoreResult(BaseModel):
    """Output of the fraud scoring engine."""

    model_config = ConfigDict(frozen=True)

    transaction_id: str
    merchant_id: str
    fraud_score: float = Field(ge=0.0, le=1.0)
    decision: FraudDecision
    model_version: str
    feature_contributions: dict[str, float] = {}
    sequence_risk_score: float = 0.0
    behavioral_anomaly_flags: list[str] = []
    scored_at: datetime
    latency_ms: float


# --- Dispute Models ---

class ChargebackEvent(BaseModel):
    """Incoming chargeback notification from payment processor."""

    chargeback_id: str
    transaction_id: str
    merchant_id: str
    card_token: str
    amount_cents: int
    currency: str = "USD"
    reason_code: str
    reason_description: str | None = None
    deadline: datetime
    received_at: datetime
    payment_processor: str
    raw_payload_hash: str = Field(description="SHA-256 of original webhook payload for audit")


class EvidenceItem(BaseModel):
    """A single piece of evidence for dispute response."""

    evidence_type: str  # receipt, delivery_confirmation, avs_match, 3ds_result, behavioral
    description: str
    content: str | None = None
    attachment_url: str | None = None
    strength: EvidenceStrength
    source: str  # system, payment_processor, merchant, llm_generated
    collected_at: datetime


class DisputeCase(BaseModel):
    """Full dispute case with evidence and decision trail."""

    dispute_id: str
    chargeback_id: str
    transaction_id: str
    merchant_id: str
    amount_cents: int
    reason_code: str
    status: DisputeStatus = DisputeStatus.RECEIVED
    fraud_score: float | None = None
    evidence_items: list[EvidenceItem] = []
    evidence_strength: EvidenceStrength | None = None
    dispute_letter: str | None = None
    decision_rationale: str | None = None
    submitted_at: datetime | None = None
    resolved_at: datetime | None = None
    outcome: str | None = None  # won, lost, null
    human_reviewer_id: str | None = None
    created_at: datetime
    updated_at: datetime
    audit_trail: list[dict[str, Any]] = []


# --- Agent Workflow State ---

class AgentWorkflowState(BaseModel):
    """State passed through the LangGraph agent workflow."""

    dispute_id: str
    chargeback_event: ChargebackEvent
    transaction: TransactionEvent | None = None
    fraud_score_result: FraudScoreResult | None = None
    evidence_items: list[EvidenceItem] = []
    evidence_strength: EvidenceStrength | None = None
    dispute_letter: str | None = None
    decision: str | None = None  # auto_submit, human_review, decline
    decision_rationale: str | None = None
    errors: list[str] = []
    current_step: str = "initialized"
    started_at: datetime | None = None
    completed_at: datetime | None = None


# --- API Request/Response Models ---

class ScoreTransactionRequest(BaseModel):
    """API request to score a transaction."""

    transaction: TransactionEvent
    include_feature_contributions: bool = False


class ScoreTransactionResponse(BaseModel):
    """API response with fraud score and decision."""

    score: FraudScoreResult
    request_id: str


class DisputeSubmitResponse(BaseModel):
    """API response after dispute submission."""

    dispute_id: str
    status: DisputeStatus
    message: str


class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str
    model_version: str
    uptime_seconds: float
