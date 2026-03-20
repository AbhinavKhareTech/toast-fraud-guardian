"""
Security utilities: PII masking, token-only processing, audit context.
CRITICAL: No PAN or raw card data is ever stored or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

# Patterns that should NEVER appear in logs or storage
_PAN_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_CVV_PATTERN = re.compile(r"\b\d{3,4}\b")
_SSN_PATTERN = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")

_SENSITIVE_FIELDS = frozenset({
    "card_number", "pan", "cvv", "cvc", "security_code",
    "ssn", "social_security", "password", "secret",
    "account_number", "routing_number",
})


class AuditContext(BaseModel):
    """Immutable audit context attached to every decision and action."""

    trace_id: str
    request_id: str
    merchant_id: str
    timestamp: datetime
    actor: str  # "system", "agent:<name>", "human:<user_id>"
    action: str
    resource_type: str
    resource_id: str
    metadata: dict[str, Any] = {}

    @classmethod
    def create(
        cls,
        *,
        trace_id: str,
        merchant_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> AuditContext:
        return cls(
            trace_id=trace_id,
            request_id=generate_request_id(),
            merchant_id=merchant_id,
            timestamp=datetime.now(timezone.utc),
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata or {},
        )


def generate_request_id() -> str:
    """Generate a cryptographically random request ID."""
    return f"req_{secrets.token_urlsafe(16)}"


def mask_pan(value: str) -> str:
    """Mask a PAN to show only last 4 digits. Returns masked form only."""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 8:
        return "****"
    return f"****-****-****-{digits[-4:]}"


def tokenize_pan(pan: str, merchant_id: str) -> str:
    """
    Create a deterministic, non-reversible token from PAN + merchant context.
    NOT a substitute for a proper tokenization vault (Stripe, Basis Theory, etc).
    Used only for internal correlation.
    """
    normalized = re.sub(r"\D", "", pan)
    payload = f"{merchant_id}:{normalized}".encode()
    return f"tok_{hashlib.sha256(payload).hexdigest()[:24]}"


def strip_pii(data: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively strip sensitive fields from a dictionary.
    Used before logging or storing any external payload.
    """
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        lower_key = key.lower()
        if lower_key in _SENSITIVE_FIELDS:
            cleaned[key] = "[REDACTED]"
        elif isinstance(value, str) and _PAN_PATTERN.search(value):
            cleaned[key] = mask_pan(value)
        elif isinstance(value, dict):
            cleaned[key] = strip_pii(value)
        elif isinstance(value, list):
            cleaned[key] = [
                strip_pii(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


def verify_webhook_signature(
    payload: bytes,
    signature: str,
    secret: str,
    algorithm: str = "sha256",
) -> bool:
    """Verify HMAC signature for incoming webhooks."""
    expected = hmac.new(
        secret.encode(),
        payload,
        getattr(hashlib, algorithm),
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@contextmanager
def audit_span(
    trace_id: str,
    merchant_id: str,
    actor: str,
    action: str,
) -> Generator[dict[str, Any], None, None]:
    """Context manager that logs audit events on entry and exit."""
    context: dict[str, Any] = {
        "trace_id": trace_id,
        "merchant_id": merchant_id,
        "actor": actor,
        "action": action,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("audit.action.started", **context)
    try:
        yield context
    except Exception as exc:
        context["error"] = str(exc)
        context["status"] = "failed"
        logger.error("audit.action.failed", **context)
        raise
    else:
        context["status"] = "completed"
        context["completed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("audit.action.completed", **context)
