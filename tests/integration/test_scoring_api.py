"""
Integration tests for the scoring API endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import FraudDecision, FraudScoreResult


class TestScoringAPI:
    @pytest.fixture
    def client(self):
        with patch("app.main.init_db", new_callable=AsyncMock), \
             patch("app.main.close_db", new_callable=AsyncMock), \
             patch("app.main.close_all_pools", new_callable=AsyncMock):
            from app.main import app
            return TestClient(app)

    def _make_score_request(self) -> dict:
        return {
            "transaction": {
                "transaction_id": "txn_api_test_001",
                "merchant_id": "merchant_rest_042",
                "card_token": "tok_test_api_abc",
                "amount_cents": 3500,
                "currency": "USD",
                "transaction_type": "card_present",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payment_processor": "stripe",
            },
            "include_feature_contributions": True,
        }

    @patch("app.api.scoring.get_scoring_engine")
    def test_score_transaction_success(self, mock_engine_fn, client):
        mock_engine = MagicMock()
        mock_score = FraudScoreResult(
            transaction_id="txn_api_test_001",
            merchant_id="merchant_rest_042",
            fraud_score=0.15,
            decision=FraudDecision.APPROVE,
            model_version="v1.0.0-test",
            feature_contributions={"amount_log": 0.85},
            scored_at=datetime.now(timezone.utc),
            latency_ms=23.5,
        )
        mock_engine.score_transaction = AsyncMock(return_value=mock_score)
        mock_engine_fn.return_value = mock_engine

        resp = client.post("/api/v1/scoring/score", json=self._make_score_request())
        assert resp.status_code == 200

        data = resp.json()
        assert data["score"]["fraud_score"] == 0.15
        assert data["score"]["decision"] == "approve"
        assert data["score"]["model_version"] == "v1.0.0-test"
        assert "request_id" in data
        assert data["score"]["feature_contributions"]["amount_log"] == 0.85

    @patch("app.api.scoring.get_scoring_engine")
    def test_score_without_feature_contributions(self, mock_engine_fn, client):
        mock_engine = MagicMock()
        mock_score = FraudScoreResult(
            transaction_id="txn_api_test_002",
            merchant_id="m1",
            fraud_score=0.50,
            decision=FraudDecision.REVIEW,
            model_version="v1.0.0",
            feature_contributions={"amount_log": 0.85, "hour_sin": 0.2},
            scored_at=datetime.now(timezone.utc),
            latency_ms=30.0,
        )
        mock_engine.score_transaction = AsyncMock(return_value=mock_score)
        mock_engine_fn.return_value = mock_engine

        req = self._make_score_request()
        req["include_feature_contributions"] = False
        resp = client.post("/api/v1/scoring/score", json=req)
        assert resp.status_code == 200

        data = resp.json()
        assert data["score"]["feature_contributions"] == {}

    def test_score_rejects_raw_pan(self, client):
        req = self._make_score_request()
        req["transaction"]["card_token"] = "4111111111111111"

        resp = client.post("/api/v1/scoring/score", json=req)
        assert resp.status_code == 422  # Validation error

    def test_score_rejects_zero_amount(self, client):
        req = self._make_score_request()
        req["transaction"]["amount_cents"] = 0

        resp = client.post("/api/v1/scoring/score", json=req)
        assert resp.status_code == 422

    @patch("app.api.scoring.get_scoring_engine")
    def test_scoring_health(self, mock_engine_fn, client):
        mock_engine = MagicMock()
        mock_engine.is_ready = True
        mock_engine._active_version = "v1.0.0"
        mock_engine_fn.return_value = mock_engine

        resp = client.get("/api/v1/scoring/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True
        assert data["active_version"] == "v1.0.0"

    def test_request_id_header(self, client):
        resp = client.get("/health")
        assert "x-request-id" in resp.headers
        assert "x-response-time-ms" in resp.headers
