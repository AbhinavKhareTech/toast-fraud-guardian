"""
Abstract payment adapter interface.
All payment processor integrations implement this contract.
Supports sandbox mode, idempotency, and structured error handling.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AdapterMode(str, Enum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


@dataclass(frozen=True)
class PaymentTransaction:
    """Normalized transaction from any payment processor."""

    transaction_id: str
    merchant_id: str
    card_token: str
    amount_cents: int
    currency: str
    transaction_type: str
    timestamp: datetime
    processor: str
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DisputeSubmission:
    """Normalized dispute submission to a payment processor."""

    dispute_id: str
    chargeback_id: str
    transaction_id: str
    evidence_text: str
    evidence_attachments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DisputeSubmissionResult:
    """Result of dispute submission attempt."""

    success: bool
    processor_dispute_id: str | None = None
    status: str = "unknown"
    message: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


class PaymentAdapterError(Exception):
    """Base error for payment adapter operations."""

    def __init__(self, message: str, processor: str, retryable: bool = False):
        self.processor = processor
        self.retryable = retryable
        super().__init__(f"[{processor}] {message}")


class PaymentAdapter(ABC):
    """
    Abstract base for payment processor integrations.
    Each adapter normalizes processor-specific data into our domain models.
    """

    @property
    @abstractmethod
    def processor_name(self) -> str:
        """Unique identifier for this processor (e.g., 'stripe', 'square', 'toast')."""
        ...

    @abstractmethod
    async def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> bool:
        """Verify webhook signature authenticity."""
        ...

    @abstractmethod
    async def parse_chargeback_webhook(
        self, payload: dict[str, Any]
    ) -> PaymentTransaction | None:
        """Parse a chargeback webhook into a normalized transaction."""
        ...

    @abstractmethod
    async def fetch_transaction(self, transaction_id: str) -> PaymentTransaction | None:
        """Fetch transaction details from the processor."""
        ...

    @abstractmethod
    async def submit_dispute_evidence(
        self, submission: DisputeSubmission
    ) -> DisputeSubmissionResult:
        """Submit dispute evidence to the processor. Must be idempotent."""
        ...

    @abstractmethod
    async def get_dispute_status(self, processor_dispute_id: str) -> dict[str, Any]:
        """Check current dispute status from the processor."""
        ...
