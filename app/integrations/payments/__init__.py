"""
Payment adapter registry. Maps processor names to adapter instances.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.integrations.payments.base import PaymentAdapter
from app.integrations.payments.square_adapter import SquareAdapter
from app.integrations.payments.stripe_adapter import StripeAdapter
from app.integrations.payments.toast_adapter import ToastAdapter

_registry: dict[str, PaymentAdapter] = {}


def get_adapter(processor: str) -> PaymentAdapter:
    """Get or create a payment adapter by processor name."""
    if processor not in _registry:
        settings = get_settings()
        if processor == "stripe":
            _registry[processor] = StripeAdapter()
        elif processor == "square":
            _registry[processor] = SquareAdapter()
        elif processor == "toast":
            _registry[processor] = ToastAdapter(
                client_id=settings.toast_client_id,
                client_secret=settings.toast_client_secret.get_secret_value(),
                environment=settings.toast_environment,
            )
        else:
            raise ValueError(f"Unknown payment processor: {processor}")
    return _registry[processor]


def list_adapters() -> list[str]:
    return ["stripe", "square", "toast"]
