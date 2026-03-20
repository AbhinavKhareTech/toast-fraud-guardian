"""
Unit tests for LLM service: prompt template validation, client factory, response handling.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm_service import (
    DISPUTE_LETTER_SYSTEM_PROMPT,
    DISPUTE_LETTER_USER_TEMPLATE,
    EVIDENCE_SUMMARY_SYSTEM_PROMPT,
    EVIDENCE_SUMMARY_USER_TEMPLATE,
    AnthropicClient,
    LLMResponse,
    OpenAIClient,
    get_llm_client,
)


class TestPromptTemplates:
    """Verify prompt templates are well-formed and contain no CoT leakage."""

    def test_dispute_letter_system_prompt_no_cot(self):
        prompt = DISPUTE_LETTER_SYSTEM_PROMPT
        assert "chain of thought" not in prompt.lower()
        assert "think step by step" not in prompt.lower()
        assert "let me think" not in prompt.lower()
        # Should instruct structured output
        assert "ONLY the dispute letter" in prompt

    def test_dispute_letter_user_template_placeholders(self):
        template = DISPUTE_LETTER_USER_TEMPLATE
        required_placeholders = [
            "{transaction_id}", "{merchant_name}", "{merchant_id}",
            "{amount:.2f}", "{transaction_date}", "{transaction_type}",
            "{reason_code}", "{reason_description}",
            "{evidence_summary}", "{fraud_score:.2f}", "{behavioral_signals}",
        ]
        for placeholder in required_placeholders:
            # Check the base name exists in the template
            base_name = placeholder.split(":")[0].strip("{}")
            assert f"{{{base_name}" in template, f"Missing placeholder: {placeholder}"

    def test_dispute_letter_template_renders(self):
        rendered = DISPUTE_LETTER_USER_TEMPLATE.format(
            transaction_id="txn_001",
            merchant_name="Test Restaurant",
            merchant_id="m_001",
            amount=45.99,
            transaction_date="2025-06-15",
            transaction_type="card_present",
            reason_code="10.4",
            reason_description="Fraud - Card Absent",
            evidence_summary="- AVS match\n- Tip left",
            fraud_score=0.12,
            behavioral_signals="None detected",
        )
        assert "txn_001" in rendered
        assert "$45.99" in rendered
        assert "10.4" in rendered

    def test_evidence_summary_system_prompt_structured(self):
        prompt = EVIDENCE_SUMMARY_SYSTEM_PROMPT
        assert "JSON" in prompt
        assert "strength" in prompt
        assert "recommendation" in prompt

    def test_evidence_summary_template_renders(self):
        rendered = EVIDENCE_SUMMARY_USER_TEMPLATE.format(
            reason_code="10.4",
            reason_description="Fraud",
            amount=45.99,
            evidence_items_text="- AVS matched\n- CVV matched",
            fraud_score=0.12,
            anomaly_flags="None",
        )
        assert "$45.99" in rendered
        assert "AVS matched" in rendered


class TestLLMResponse:
    def test_response_attributes(self):
        resp = LLMResponse(
            content="Test response",
            model="claude-test",
            usage_tokens={"input": 100, "output": 50},
            latency_ms=500.0,
        )
        assert resp.content == "Test response"
        assert resp.model == "claude-test"
        assert resp.usage_tokens["input"] == 100
        assert resp.latency_ms == 500.0


class TestClientFactory:
    @patch("app.services.llm_service.get_settings")
    def test_anthropic_client_created(self, mock_settings):
        mock_settings.return_value.llm_provider = "anthropic"
        mock_settings.return_value.anthropic_api_key = MagicMock(get_secret_value=lambda: "test-key")
        mock_settings.return_value.llm_model = "claude-test"
        client = get_llm_client()
        assert isinstance(client, AnthropicClient)

    @patch("app.services.llm_service.get_settings")
    def test_openai_client_created(self, mock_settings):
        mock_settings.return_value.llm_provider = "openai"
        mock_settings.return_value.openai_api_key = MagicMock(get_secret_value=lambda: "test-key")
        client = get_llm_client()
        assert isinstance(client, OpenAIClient)

    @patch("app.services.llm_service.get_settings")
    def test_unknown_provider_raises(self, mock_settings):
        mock_settings.return_value.llm_provider = "unknown"
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            get_llm_client()
