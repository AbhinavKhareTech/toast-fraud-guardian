"""
Integration tests for the end-to-end webhook -> agent -> submission flow.
Uses mocked external services (payment processors, LLM, Redis).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


class TestWebhookToDisputeFlow:
    """Test the full webhook ingestion and dispute creation flow."""

    @pytest.fixture
    def client(self):
        """Create a test client with mocked dependencies."""
        with patch("app.main.init_db", new_callable=AsyncMock), \
             patch("app.main.close_db", new_callable=AsyncMock), \
             patch("app.main.close_all_pools", new_callable=AsyncMock):
            from app.main import app
            return TestClient(app)

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "model_version" in data

    def test_metrics_endpoint(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "fraud_score_latency" in resp.text

    def test_unknown_processor_webhook_rejected(self, client):
        resp = client.post(
            "/api/v1/disputes/webhooks/unknown_processor",
            json={"test": "data"},
        )
        assert resp.status_code == 400

    @patch("app.api.disputes.get_adapter")
    def test_invalid_signature_rejected(self, mock_get_adapter, client):
        mock_adapter = AsyncMock()
        mock_adapter.verify_webhook = AsyncMock(return_value=False)
        mock_get_adapter.return_value = mock_adapter

        resp = client.post(
            "/api/v1/disputes/webhooks/stripe",
            content=b'{"test": "data"}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 401


class TestDisputeWorkflowIntegration:
    """Test the agent workflow with mocked services."""

    @pytest.mark.asyncio
    async def test_full_workflow_auto_submit(self, sample_chargeback):
        """End-to-end: chargeback -> score -> evidence -> letter -> auto-submit."""
        from app.agents.dispute_workflow import run_dispute_workflow
        from app.integrations.payments.base import (
            DisputeSubmissionResult,
            PaymentTransaction,
        )

        mock_txn = PaymentTransaction(
            transaction_id="txn_test_001",
            merchant_id="merchant_rest_042",
            card_token="tok_test_abc123",
            amount_cents=4599,
            currency="USD",
            transaction_type="card_present",
            timestamp=datetime.now(timezone.utc),
            processor="stripe",
            raw_metadata={"avs_check": "Y", "cvc_check": "pass"},
        )

        mock_submission_result = DisputeSubmissionResult(
            success=True,
            processor_dispute_id="dp_stripe_001",
            status="submitted",
        )

        with patch("app.agents.dispute_workflow.get_adapter") as mock_adapter_fn, \
             patch("app.agents.dispute_workflow.get_scoring_engine") as mock_engine_fn, \
             patch("app.agents.dispute_workflow.get_llm_client") as mock_llm_fn, \
             patch("app.agents.dispute_workflow.get_settings") as mock_settings:

            # Mock adapter
            mock_adapter = AsyncMock()
            mock_adapter.fetch_transaction = AsyncMock(return_value=mock_txn)
            mock_adapter.submit_dispute_evidence = AsyncMock(return_value=mock_submission_result)
            mock_adapter_fn.return_value = mock_adapter

            # Mock scoring engine
            from app.models.schemas import FraudDecision, FraudScoreResult
            mock_score = FraudScoreResult(
                transaction_id="txn_test_001",
                merchant_id="merchant_rest_042",
                fraud_score=0.08,
                decision=FraudDecision.APPROVE,
                model_version="v1.0.0-test",
                scored_at=datetime.now(timezone.utc),
                latency_ms=25.0,
            )
            mock_engine = MagicMock()
            mock_engine.score_transaction = AsyncMock(return_value=mock_score)
            mock_engine_fn.return_value = mock_engine

            # Mock LLM
            from app.services.llm_service import LLMResponse
            mock_llm = AsyncMock()
            mock_llm.generate = AsyncMock(return_value=LLMResponse(
                content="Dear Card Network, We dispute this chargeback...",
                model="test-model",
                usage_tokens={"input": 500, "output": 200},
                latency_ms=1200.0,
            ))
            mock_llm_fn.return_value = mock_llm

            # Mock settings
            mock_settings.return_value.ff_auto_submit_disputes = True
            mock_settings.return_value.ff_llm_evidence_writer = True
            mock_settings.return_value.fraud_score_threshold_auto = 0.85

            # Run workflow
            final_state = await run_dispute_workflow("disp_test_int_001", sample_chargeback)

            # Assertions
            assert final_state["decision"] == "auto_submit"
            assert final_state["fraud_score_result"] is not None
            assert final_state["fraud_score_result"]["fraud_score"] == 0.08
            assert len(final_state["evidence_items"]) > 0
            assert final_state["dispute_letter"] is not None
            assert final_state["current_step"] == "submitted"
            assert not final_state["errors"]

    @pytest.mark.asyncio
    async def test_workflow_human_review_on_error(self, sample_chargeback):
        """Workflow routes to human review when transaction fetch fails."""
        from app.agents.dispute_workflow import run_dispute_workflow

        with patch("app.agents.dispute_workflow.get_adapter") as mock_adapter_fn, \
             patch("app.agents.dispute_workflow.get_settings") as mock_settings:

            mock_adapter = AsyncMock()
            mock_adapter.fetch_transaction = AsyncMock(return_value=None)
            mock_adapter_fn.return_value = mock_adapter

            mock_settings.return_value.ff_auto_submit_disputes = True
            mock_settings.return_value.ff_llm_evidence_writer = False
            mock_settings.return_value.fraud_score_threshold_auto = 0.85

            final_state = await run_dispute_workflow("disp_err_001", sample_chargeback)

            # Should route to human review due to errors
            assert final_state["decision"] == "human_review"
            assert len(final_state["errors"]) > 0
