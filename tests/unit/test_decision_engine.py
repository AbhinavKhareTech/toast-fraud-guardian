"""
Unit tests for the decision engine and evidence assessment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.schemas import (
    ChargebackEvent,
    EvidenceItem,
    EvidenceStrength,
)
from app.services.evidence_service import EvidenceCollector


class TestEvidenceStrengthAssessment:
    """Test evidence strength calculation logic."""

    @pytest.fixture
    def collector(self):
        from unittest.mock import AsyncMock
        mock_adapter = AsyncMock()
        return EvidenceCollector(mock_adapter)

    def test_high_strength_with_strong_evidence(self, collector, sample_evidence_items):
        strength = collector.assess_evidence_strength(sample_evidence_items)
        # 2 HIGH + 1 MEDIUM with 3 items => high
        assert strength == EvidenceStrength.HIGH

    def test_insufficient_with_no_evidence(self, collector):
        strength = collector.assess_evidence_strength([])
        assert strength == EvidenceStrength.INSUFFICIENT

    def test_low_strength_with_weak_evidence(self, collector):
        now = datetime.now(timezone.utc)
        items = [
            EvidenceItem(
                evidence_type="order_receipt",
                description="Order had 2 items.",
                strength=EvidenceStrength.LOW,
                source="merchant",
                collected_at=now,
            ),
        ]
        strength = collector.assess_evidence_strength(items)
        assert strength == EvidenceStrength.LOW

    def test_medium_strength(self, collector):
        now = datetime.now(timezone.utc)
        items = [
            EvidenceItem(
                evidence_type="avs_match",
                description="AVS matched.",
                strength=EvidenceStrength.HIGH,
                source="payment_processor",
                collected_at=now,
            ),
            EvidenceItem(
                evidence_type="tip_present",
                description="Tip left.",
                strength=EvidenceStrength.LOW,
                source="merchant",
                collected_at=now,
            ),
        ]
        strength = collector.assess_evidence_strength(items)
        assert strength == EvidenceStrength.MEDIUM


class TestDecisionEngineLogic:
    """Test the agent decision engine routing logic."""

    @pytest.mark.asyncio
    async def test_auto_submit_decision(self):
        """When fraud score is low, evidence is strong, and auto-submit is enabled."""
        from app.agents.dispute_workflow import decision_engine_node

        state = {
            "dispute_id": "disp_test_001",
            "chargeback_event": {},
            "fraud_score_result": {
                "fraud_score": 0.08,
                "behavioral_anomaly_flags": [],
            },
            "evidence_strength": "high",
            "dispute_letter": "Dear Sir/Madam...",
            "errors": [],
            "current_step": "letter_generated",
        }

        with patch("app.agents.dispute_workflow.get_settings") as mock_settings:
            mock_settings.return_value.ff_auto_submit_disputes = True
            mock_settings.return_value.fraud_score_threshold_auto = 0.85
            result = await decision_engine_node(state)

        assert result["decision"] == "auto_submit"

    @pytest.mark.asyncio
    async def test_human_review_on_errors(self):
        """Errors should force human review regardless of score."""
        from app.agents.dispute_workflow import decision_engine_node

        state = {
            "dispute_id": "disp_test_002",
            "chargeback_event": {},
            "fraud_score_result": {"fraud_score": 0.05, "behavioral_anomaly_flags": []},
            "evidence_strength": "high",
            "dispute_letter": "Dear...",
            "errors": ["Transaction not found in processor"],
            "current_step": "letter_generated",
        }

        with patch("app.agents.dispute_workflow.get_settings") as mock_settings:
            mock_settings.return_value.ff_auto_submit_disputes = True
            mock_settings.return_value.fraud_score_threshold_auto = 0.85
            result = await decision_engine_node(state)

        assert result["decision"] == "human_review"

    @pytest.mark.asyncio
    async def test_human_review_when_feature_flag_disabled(self):
        """Auto-submit disabled via feature flag."""
        from app.agents.dispute_workflow import decision_engine_node

        state = {
            "dispute_id": "disp_test_003",
            "chargeback_event": {},
            "fraud_score_result": {"fraud_score": 0.05, "behavioral_anomaly_flags": []},
            "evidence_strength": "high",
            "dispute_letter": "Dear...",
            "errors": [],
            "current_step": "letter_generated",
        }

        with patch("app.agents.dispute_workflow.get_settings") as mock_settings:
            mock_settings.return_value.ff_auto_submit_disputes = False
            mock_settings.return_value.fraud_score_threshold_auto = 0.85
            result = await decision_engine_node(state)

        assert result["decision"] == "human_review"
        assert "feature flag" in result["decision_rationale"].lower()

    @pytest.mark.asyncio
    async def test_human_review_on_weak_evidence(self):
        """Weak evidence routes to human review."""
        from app.agents.dispute_workflow import decision_engine_node

        state = {
            "dispute_id": "disp_test_004",
            "chargeback_event": {},
            "fraud_score_result": {"fraud_score": 0.1, "behavioral_anomaly_flags": []},
            "evidence_strength": "low",
            "dispute_letter": "Dear...",
            "errors": [],
            "current_step": "letter_generated",
        }

        with patch("app.agents.dispute_workflow.get_settings") as mock_settings:
            mock_settings.return_value.ff_auto_submit_disputes = True
            mock_settings.return_value.fraud_score_threshold_auto = 0.85
            result = await decision_engine_node(state)

        assert result["decision"] == "human_review"


class TestWorkflowRouting:
    """Test the conditional routing after decision engine."""

    def test_route_auto_submit(self):
        from app.agents.dispute_workflow import route_after_decision
        state = {"decision": "auto_submit"}
        assert route_after_decision(state) == "dispute_submitter"

    def test_route_human_review_ends(self):
        from app.agents.dispute_workflow import END, route_after_decision
        state = {"decision": "human_review"}
        assert route_after_decision(state) == END

    def test_route_decline_ends(self):
        from app.agents.dispute_workflow import END, route_after_decision
        state = {"decision": "decline_dispute"}
        assert route_after_decision(state) == END
