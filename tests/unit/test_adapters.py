"""
Unit tests for payment adapter implementations.
Tests webhook verification, transaction parsing, and evidence submission.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.payments.base import DisputeSubmission
from app.integrations.payments.toast_adapter import ToastAdapter


class TestToastAdapterSandbox:
    """Test Toast adapter sandbox mode (mock implementations)."""

    @pytest.fixture
    def adapter(self):
        return ToastAdapter(environment="sandbox")

    @pytest.mark.asyncio
    async def test_sandbox_webhook_verification(self, adapter):
        result = await adapter.verify_webhook(b"payload", {})
        assert result is True  # sandbox always accepts

    @pytest.mark.asyncio
    async def test_sandbox_parse_chargeback(self, adapter):
        payload = {
            "paymentGuid": "toast_pay_001",
            "restaurantGuid": "rest_42",
            "amount": 45.99,
        }
        txn = await adapter.parse_chargeback_webhook(payload)
        assert txn is not None
        assert txn.processor == "toast"
        assert txn.merchant_id == "rest_42"
        assert txn.amount_cents == 4599

    @pytest.mark.asyncio
    async def test_sandbox_fetch_transaction(self, adapter):
        txn = await adapter.fetch_transaction("toast_txn_001")
        assert txn is not None
        assert txn.transaction_id == "toast_txn_001"
        assert txn.processor == "toast"
        assert txn.transaction_type == "card_present"

    @pytest.mark.asyncio
    async def test_sandbox_submit_evidence(self, adapter):
        submission = DisputeSubmission(
            dispute_id="disp_001",
            chargeback_id="cb_001",
            transaction_id="txn_001",
            evidence_text="Test evidence",
        )
        result = await adapter.submit_dispute_evidence(submission)
        assert result.success is True
        assert "SANDBOX" in result.message

    @pytest.mark.asyncio
    async def test_production_raises_not_implemented(self):
        adapter = ToastAdapter(environment="production")
        with pytest.raises(NotImplementedError):
            await adapter.fetch_transaction("txn_001")

    def test_processor_name(self, adapter):
        assert adapter.processor_name == "toast"


class TestStripeAdapterWebhook:
    """Test Stripe webhook signature verification."""

    @pytest.mark.asyncio
    async def test_valid_stripe_signature(self):
        from app.integrations.payments.stripe_adapter import StripeAdapter

        secret = "whsec_test_secret_123"
        adapter = StripeAdapter(api_key="sk_test_fake", webhook_secret=secret)

        payload = b'{"id": "evt_test", "type": "charge.dispute.created"}'
        timestamp = str(int(time.time()))
        signed = f"{timestamp}.{payload.decode()}".encode()
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        header = f"t={timestamp},v1={sig}"

        result = await adapter.verify_webhook(payload, {"stripe-signature": header})
        assert result is True

    @pytest.mark.asyncio
    async def test_expired_stripe_signature(self):
        from app.integrations.payments.stripe_adapter import StripeAdapter

        secret = "whsec_test_secret_123"
        adapter = StripeAdapter(api_key="sk_test_fake", webhook_secret=secret)

        payload = b'{"id": "evt_test"}'
        old_timestamp = str(int(time.time()) - 600)  # 10 min ago
        signed = f"{old_timestamp}.{payload.decode()}".encode()
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        header = f"t={old_timestamp},v1={sig}"

        result = await adapter.verify_webhook(payload, {"stripe-signature": header})
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_signature_header(self):
        from app.integrations.payments.stripe_adapter import StripeAdapter

        adapter = StripeAdapter(api_key="sk_test_fake", webhook_secret="secret")
        result = await adapter.verify_webhook(b"payload", {})
        assert result is False


class TestAdapterRegistry:
    def test_list_adapters(self):
        from app.integrations.payments import list_adapters
        adapters = list_adapters()
        assert "stripe" in adapters
        assert "square" in adapters
        assert "toast" in adapters

    def test_unknown_adapter_raises(self):
        from app.integrations.payments import get_adapter
        with pytest.raises(ValueError, match="Unknown payment processor"):
            get_adapter("unknown_processor")
