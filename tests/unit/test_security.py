"""
Unit tests for security utilities: PII masking, tokenization, webhook verification.
"""

from __future__ import annotations

import pytest

from app.core.security import (
    mask_pan,
    strip_pii,
    tokenize_pan,
    verify_webhook_signature,
)


class TestPANMasking:
    def test_mask_standard_pan(self):
        assert mask_pan("4111111111111111") == "****-****-****-1111"

    def test_mask_pan_with_spaces(self):
        assert mask_pan("4111 1111 1111 1111") == "****-****-****-1111"

    def test_mask_short_value(self):
        assert mask_pan("1234") == "****"

    def test_mask_amex(self):
        assert mask_pan("378282246310005") == "****-****-****-0005"


class TestTokenization:
    def test_deterministic(self):
        t1 = tokenize_pan("4111111111111111", "merchant_001")
        t2 = tokenize_pan("4111111111111111", "merchant_001")
        assert t1 == t2

    def test_different_merchants_different_tokens(self):
        t1 = tokenize_pan("4111111111111111", "merchant_001")
        t2 = tokenize_pan("4111111111111111", "merchant_002")
        assert t1 != t2

    def test_token_format(self):
        token = tokenize_pan("4111111111111111", "m1")
        assert token.startswith("tok_")
        assert len(token) == 28  # tok_ + 24 hex chars


class TestPIIStripping:
    def test_strips_sensitive_fields(self):
        data = {
            "card_number": "4111111111111111",
            "cvv": "123",
            "name": "John Doe",
            "amount": 100,
        }
        cleaned = strip_pii(data)
        assert cleaned["card_number"] == "[REDACTED]"
        assert cleaned["cvv"] == "[REDACTED]"
        assert cleaned["name"] == "John Doe"
        assert cleaned["amount"] == 100

    def test_strips_nested_pii(self):
        data = {
            "payment": {
                "card_number": "4111111111111111",
                "amount": 50,
            },
            "merchant_id": "m1",
        }
        cleaned = strip_pii(data)
        assert cleaned["payment"]["card_number"] == "[REDACTED]"
        assert cleaned["payment"]["amount"] == 50

    def test_masks_pan_in_values(self):
        data = {"description": "Charge on card 4111111111111111 for order"}
        cleaned = strip_pii(data)
        assert "4111111111111111" not in cleaned["description"]
        assert "****" in cleaned["description"]

    def test_strips_list_items(self):
        data = {
            "items": [
                {"card_number": "4111", "name": "test"},
                {"amount": 100},
            ]
        }
        cleaned = strip_pii(data)
        assert cleaned["items"][0]["card_number"] == "[REDACTED]"
        assert cleaned["items"][1]["amount"] == 100


class TestWebhookVerification:
    def test_valid_signature(self):
        import hashlib
        import hmac
        payload = b'{"test": "data"}'
        secret = "my_secret"
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        payload = b'{"test": "data"}'
        assert verify_webhook_signature(payload, "invalid_sig", "secret") is False

    def test_tampered_payload(self):
        import hashlib
        import hmac
        original = b'{"amount": 100}'
        secret = "my_secret"
        sig = hmac.new(secret.encode(), original, hashlib.sha256).hexdigest()
        tampered = b'{"amount": 10000}'
        assert verify_webhook_signature(tampered, sig, secret) is False
