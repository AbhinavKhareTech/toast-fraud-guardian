"""
Unit tests for Pydantic model validation.
Ensures PCI-safe patterns are enforced.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    ChargebackEvent,
    FraudDecision,
    FraudScoreResult,
    TransactionEvent,
    TransactionType,
)


class TestTransactionEvent:
    def test_rejects_raw_pan_as_token(self):
        """Card token must be a processor token, not raw PAN."""
        with pytest.raises(ValidationError, match="card_token must be a processor token"):
            TransactionEvent(
                transaction_id="txn_1",
                merchant_id="m1",
                card_token="4111111111111111",  # Raw PAN!
                amount_cents=1000,
                transaction_type=TransactionType.CARD_PRESENT,
                timestamp=datetime.now(timezone.utc),
                payment_processor="stripe",
            )

    def test_accepts_valid_token(self):
        txn = TransactionEvent(
            transaction_id="txn_1",
            merchant_id="m1",
            card_token="tok_abc123",
            amount_cents=1000,
            transaction_type=TransactionType.CARD_PRESENT,
            timestamp=datetime.now(timezone.utc),
            payment_processor="stripe",
        )
        assert txn.card_token == "tok_abc123"

    def test_accepts_card_prefix_token(self):
        txn = TransactionEvent(
            transaction_id="txn_1",
            merchant_id="m1",
            card_token="card_abc123",
            amount_cents=1000,
            transaction_type=TransactionType.CARD_PRESENT,
            timestamp=datetime.now(timezone.utc),
            payment_processor="stripe",
        )
        assert txn.card_token == "card_abc123"

    def test_rejects_zero_amount(self):
        with pytest.raises(ValidationError):
            TransactionEvent(
                transaction_id="txn_1",
                merchant_id="m1",
                card_token="tok_abc",
                amount_cents=0,
                transaction_type=TransactionType.CARD_PRESENT,
                timestamp=datetime.now(timezone.utc),
                payment_processor="stripe",
            )

    def test_frozen_model(self, sample_transaction):
        with pytest.raises(ValidationError):
            sample_transaction.amount_cents = 9999


class TestFraudScoreResult:
    def test_score_bounds(self):
        with pytest.raises(ValidationError):
            FraudScoreResult(
                transaction_id="t1",
                merchant_id="m1",
                fraud_score=1.5,  # out of bounds
                decision=FraudDecision.DECLINE,
                model_version="v1",
                scored_at=datetime.now(timezone.utc),
                latency_ms=10.0,
            )

    def test_valid_score(self, sample_fraud_score):
        assert 0.0 <= sample_fraud_score.fraud_score <= 1.0
        assert sample_fraud_score.decision == FraudDecision.APPROVE


class TestChargebackEvent:
    def test_valid_chargeback(self, sample_chargeback):
        assert sample_chargeback.chargeback_id == "cb_test_001"
        assert sample_chargeback.payment_processor == "stripe"
        assert sample_chargeback.amount_cents == 4599
