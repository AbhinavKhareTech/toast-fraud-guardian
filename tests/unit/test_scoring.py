"""
Unit tests for fraud scoring engine.
Tests heuristic scoring, decision thresholds, anomaly detection, and feature extraction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from app.models.schemas import (
    DeviceSignals,
    FraudDecision,
    OrderMetadata,
    TransactionEvent,
    TransactionType,
)
from ml.inference.features import (
    FEATURE_DIM,
    FeatureVector,
    SequenceFeatures,
    _extract_order_features,
    _extract_transaction_features,
)


class TestFeatureVector:
    def test_set_and_get(self):
        fv = FeatureVector()
        fv.set("amount", 100.0)
        fv.set("hour", 14.0)
        assert fv.to_dict() == {"amount": 100.0, "hour": 14.0}

    def test_to_numpy_shape(self):
        fv = FeatureVector()
        fv.set("a", 1.0)
        fv.set("b", 2.0)
        arr = fv.to_numpy()
        assert arr.shape == (FEATURE_DIM,)
        assert arr[0] == 1.0
        assert arr[1] == 2.0
        assert arr[2] == 0.0  # zero-padded

    def test_to_numpy_truncates_beyond_dim(self):
        fv = FeatureVector()
        for i in range(FEATURE_DIM + 10):
            fv.set(f"f_{i}", float(i))
        arr = fv.to_numpy()
        assert arr.shape == (FEATURE_DIM,)
        assert arr[-1] == float(FEATURE_DIM - 1)


class TestSequenceFeatures:
    def test_empty_sequence(self):
        seq = SequenceFeatures()
        arr = seq.to_padded_array()
        assert arr.shape == (20, 4)
        assert np.all(arr == 0)

    def test_partial_sequence(self):
        seq = SequenceFeatures()
        seq.amounts = [100.0, 200.0, 300.0]
        seq.time_deltas = [0.0, 60.0, 120.0]
        seq.merchant_categories = [5812, 5812, 5813]
        seq.channels = [0, 1, 0]
        seq.length = 3
        arr = seq.to_padded_array()
        assert arr[0, 0] == 100.0
        assert arr[2, 1] == 120.0
        assert arr[3, 0] == 0.0  # padded


class TestTransactionFeatureExtraction:
    def test_basic_features(self, sample_transaction):
        fv = FeatureVector()
        _extract_transaction_features(sample_transaction, fv)
        d = fv.to_dict()

        assert d["amount_cents"] == 4599.0
        assert d["amount_log"] == pytest.approx(np.log1p(4599), abs=0.01)
        assert d["is_weekend"] == 0.0  # June 15 2025 is Sunday actually
        assert d["is_card_not_present"] == 0.0  # card_present
        assert "hour_sin" in d
        assert "hour_cos" in d

    def test_cnp_detection(self):
        txn = TransactionEvent(
            transaction_id="txn_cnp",
            merchant_id="m1",
            card_token="tok_test",
            amount_cents=1000,
            transaction_type=TransactionType.ONLINE,
            timestamp=datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc),
            payment_processor="stripe",
        )
        fv = FeatureVector()
        _extract_transaction_features(txn, fv)
        assert fv.to_dict()["is_card_not_present"] == 1.0

    def test_late_night_flag(self):
        txn = TransactionEvent(
            transaction_id="txn_late",
            merchant_id="m1",
            card_token="tok_test",
            amount_cents=1000,
            transaction_type=TransactionType.CARD_PRESENT,
            timestamp=datetime(2025, 6, 15, 3, 0, tzinfo=timezone.utc),
            payment_processor="stripe",
        )
        fv = FeatureVector()
        _extract_transaction_features(txn, fv)
        assert fv.to_dict()["is_late_night"] == 1.0


class TestOrderFeatureExtraction:
    def test_order_features(self, sample_transaction):
        fv = FeatureVector()
        _extract_order_features(sample_transaction, fv)
        d = fv.to_dict()

        assert d["item_count"] == 3.0
        assert d["has_alcohol"] == 1.0
        assert d["has_tip"] == 1.0
        assert d["tip_percentage"] == 20.0
        assert d["price_per_item"] == pytest.approx(4599 / 3, abs=1)

    def test_no_order_metadata(self):
        txn = TransactionEvent(
            transaction_id="txn_no_order",
            merchant_id="m1",
            card_token="tok_test",
            amount_cents=1000,
            transaction_type=TransactionType.CARD_PRESENT,
            timestamp=datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc),
            payment_processor="stripe",
        )
        fv = FeatureVector()
        _extract_order_features(txn, fv)
        assert len(fv) == 0


class TestHeuristicScoring:
    """Test the heuristic fallback scorer in the scoring engine."""

    @pytest.fixture
    def engine(self):
        from ml.inference.scoring_engine import FraudScoringEngine
        return FraudScoringEngine()

    def test_low_risk_transaction(self, engine, sample_transaction):
        fv = FeatureVector()
        _extract_transaction_features(sample_transaction, fv)
        score = engine._heuristic_score(fv, sample_transaction)
        # Card-present, normal amount, no proxy => low risk
        assert score < 0.3

    def test_high_risk_transaction(self, engine, high_risk_transaction):
        fv = FeatureVector()
        _extract_transaction_features(high_risk_transaction, fv)
        # Manually set device features
        fv.set("is_known_proxy", 1.0)
        fv.set("is_tor_exit", 1.0)
        fv.set("card_is_new", 1.0)
        fv.set("card_velocity_1h", 6.0)

        score = engine._heuristic_score(fv, high_risk_transaction)
        # CNP, high amount, tor, proxy, new card, high velocity => high risk
        assert score >= 0.7

    def test_decision_thresholds(self, engine):
        assert engine._make_decision(0.90) == FraudDecision.DECLINE
        assert engine._make_decision(0.60) == FraudDecision.REVIEW
        assert engine._make_decision(0.20) == FraudDecision.APPROVE

    def test_anomaly_detection(self, engine, high_risk_transaction):
        fv = FeatureVector()
        fv.set("card_velocity_1h", 10.0)
        fv.set("is_tor_exit", 1.0)
        fv.set("is_known_proxy", 1.0)
        fv.set("card_is_new", 1.0)
        fv.set("is_late_night", 1.0)
        fv.set("is_card_not_present", 1.0)

        flags = engine._detect_anomalies(fv, high_risk_transaction)
        assert "high_velocity" in flags
        assert "tor_exit_node" in flags
        assert "known_proxy" in flags
        assert "first_seen_card" in flags
        assert "high_value_transaction" in flags
        assert "late_night_cnp" in flags
