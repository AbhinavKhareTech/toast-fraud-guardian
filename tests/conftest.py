"""
Shared test fixtures and configuration.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.models.schemas import (
    ChargebackEvent,
    DeviceSignals,
    EvidenceItem,
    EvidenceStrength,
    FraudDecision,
    FraudScoreResult,
    OrderMetadata,
    TransactionEvent,
    TransactionType,
)


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_transaction() -> TransactionEvent:
    return TransactionEvent(
        transaction_id="txn_test_001",
        merchant_id="merchant_rest_042",
        card_token="tok_test_abc123",
        amount_cents=4599,
        currency="USD",
        transaction_type=TransactionType.CARD_PRESENT,
        timestamp=datetime(2025, 6, 15, 19, 30, 0, tzinfo=timezone.utc),
        payment_processor="stripe",
        device_signals=DeviceSignals(
            ip_address_hash="a1b2c3d4e5f6",
            device_fingerprint="fp_device_001",
            is_known_proxy=False,
            is_tor_exit=False,
            geo_country="US",
            geo_city="Boston",
            geo_lat=42.3601,
            geo_lng=-71.0589,
        ),
        order_metadata=OrderMetadata(
            order_id="ord_test_001",
            item_count=3,
            avg_item_price_cents=1533,
            has_alcohol=True,
            has_tip=True,
            tip_percentage=20.0,
            order_channel="in_store",
            time_to_complete_seconds=2400,
        ),
    )


@pytest.fixture
def high_risk_transaction() -> TransactionEvent:
    return TransactionEvent(
        transaction_id="txn_test_risky_001",
        merchant_id="merchant_rest_042",
        card_token="tok_test_risky_999",
        amount_cents=85000,
        currency="USD",
        transaction_type=TransactionType.CARD_NOT_PRESENT,
        timestamp=datetime(2025, 6, 15, 3, 15, 0, tzinfo=timezone.utc),
        payment_processor="stripe",
        device_signals=DeviceSignals(
            ip_address_hash="deadbeef0000",
            is_known_proxy=True,
            is_tor_exit=True,
            geo_country="XX",
        ),
        order_metadata=OrderMetadata(
            order_id="ord_risky_001",
            item_count=1,
            avg_item_price_cents=85000,
            has_alcohol=False,
            has_tip=False,
            tip_percentage=0.0,
            order_channel="online",
        ),
    )


@pytest.fixture
def sample_chargeback() -> ChargebackEvent:
    return ChargebackEvent(
        chargeback_id="cb_test_001",
        transaction_id="txn_test_001",
        merchant_id="merchant_rest_042",
        card_token="tok_test_abc123",
        amount_cents=4599,
        currency="USD",
        reason_code="10.4",
        reason_description="Fraud - Card Absent Environment",
        deadline=datetime(2025, 7, 15, 23, 59, 59, tzinfo=timezone.utc),
        received_at=datetime(2025, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
        payment_processor="stripe",
        raw_payload_hash="abc123def456",
    )


@pytest.fixture
def sample_fraud_score() -> FraudScoreResult:
    return FraudScoreResult(
        transaction_id="txn_test_001",
        merchant_id="merchant_rest_042",
        fraud_score=0.12,
        decision=FraudDecision.APPROVE,
        model_version="v1.0.0-test",
        feature_contributions={"amount_log": 0.85, "hour_sin": 0.3},
        sequence_risk_score=0.05,
        behavioral_anomaly_flags=[],
        scored_at=datetime(2025, 6, 15, 19, 30, 1, tzinfo=timezone.utc),
        latency_ms=45.2,
    )


@pytest.fixture
def sample_evidence_items() -> list[EvidenceItem]:
    now = datetime.now(timezone.utc)
    return [
        EvidenceItem(
            evidence_type="card_present_verification",
            description="Transaction was conducted with physical card present (chip).",
            strength=EvidenceStrength.HIGH,
            source="system",
            collected_at=now,
        ),
        EvidenceItem(
            evidence_type="avs_match",
            description="Address Verification Service confirmed match.",
            strength=EvidenceStrength.HIGH,
            source="payment_processor",
            collected_at=now,
        ),
        EvidenceItem(
            evidence_type="tip_present",
            description="Customer left a 20.0% tip.",
            strength=EvidenceStrength.MEDIUM,
            source="merchant",
            collected_at=now,
        ),
    ]


@pytest.fixture
def mock_redis():
    """Mock Redis client that returns empty results."""
    mock = AsyncMock()
    mock.hgetall = AsyncMock(return_value={})
    mock.zrevrange = AsyncMock(return_value=[])
    mock.zadd = AsyncMock()
    mock.zremrangebyrank = AsyncMock()
    mock.expire = AsyncMock()
    mock.ping = AsyncMock()
    return mock
