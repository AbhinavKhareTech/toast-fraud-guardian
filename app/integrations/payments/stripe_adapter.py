"""
Stripe payment adapter.
Uses Stripe's documented Disputes API for evidence submission.
Supports sandbox mode via Stripe test keys.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import get_settings
from app.integrations.payments.base import (
    DisputeSubmission,
    DisputeSubmissionResult,
    PaymentAdapter,
    PaymentAdapterError,
    PaymentTransaction,
)

logger = structlog.get_logger(__name__)

_STRIPE_API_BASE = "https://api.stripe.com/v1"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, PaymentAdapterError):
        return exc.retryable
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


class StripeAdapter(PaymentAdapter):
    """
    Stripe integration for chargeback/dispute management.
    Docs: https://docs.stripe.com/api/disputes
    """

    def __init__(self, api_key: str | None = None, webhook_secret: str | None = None):
        settings = get_settings()
        self._api_key = api_key or settings.stripe_api_key.get_secret_value()
        self._webhook_secret = webhook_secret or settings.stripe_webhook_secret.get_secret_value()
        self._client: httpx.AsyncClient | None = None

    @property
    def processor_name(self) -> str:
        return "stripe"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=_STRIPE_API_BASE,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Stripe-Version": "2024-06-20",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> bool:
        """
        Verify Stripe webhook signature using their v1 scheme.
        See: https://docs.stripe.com/webhooks/signatures
        """
        signature_header = headers.get("stripe-signature", "")
        if not signature_header or not self._webhook_secret:
            return False

        elements = dict(item.split("=", 1) for item in signature_header.split(",") if "=" in item)
        timestamp = elements.get("t", "")
        signatures = [v for k, v in elements.items() if k == "v1"]

        if not timestamp or not signatures:
            return False

        # Tolerance: 5 minutes
        if abs(time.time() - int(timestamp)) > 300:
            logger.warning("stripe.webhook.timestamp_expired", delta=abs(time.time() - int(timestamp)))
            return False

        signed_payload = f"{timestamp}.{payload.decode()}".encode()
        expected = hmac.new(
            self._webhook_secret.encode(), signed_payload, hashlib.sha256
        ).hexdigest()

        return any(hmac.compare_digest(expected, sig) for sig in signatures)

    async def parse_chargeback_webhook(
        self, payload: dict[str, Any]
    ) -> PaymentTransaction | None:
        """Parse Stripe charge.dispute.created webhook event."""
        event_type = payload.get("type", "")
        if event_type not in ("charge.dispute.created", "charge.dispute.updated"):
            return None

        dispute_obj = payload.get("data", {}).get("object", {})
        charge_id = dispute_obj.get("charge", "")

        if not charge_id:
            logger.warning("stripe.webhook.missing_charge_id", event=event_type)
            return None

        # Fetch full charge details to get transaction context
        return await self.fetch_transaction(charge_id)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def fetch_transaction(self, transaction_id: str) -> PaymentTransaction | None:
        """Fetch charge details from Stripe."""
        client = await self._get_client()
        try:
            resp = await client.get(f"/charges/{transaction_id}")
            resp.raise_for_status()
            charge = resp.json()

            return PaymentTransaction(
                transaction_id=charge["id"],
                merchant_id=charge.get("metadata", {}).get("merchant_id", "unknown"),
                card_token=charge.get("payment_method", charge.get("source", {}).get("id", "")),
                amount_cents=charge["amount"],
                currency=charge["currency"].upper(),
                transaction_type="card_present" if charge.get("payment_method_details", {}).get("type") == "card_present" else "card_not_present",
                timestamp=datetime.fromtimestamp(charge["created"], tz=timezone.utc),
                processor="stripe",
                raw_metadata=charge.get("metadata", {}),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise PaymentAdapterError(
                f"Failed to fetch charge: {e.response.status_code}",
                processor="stripe",
                retryable=e.response.status_code >= 500,
            ) from e

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def submit_dispute_evidence(
        self, submission: DisputeSubmission
    ) -> DisputeSubmissionResult:
        """
        Submit dispute evidence via Stripe Disputes API.
        Idempotent: Stripe allows updating evidence until submission.
        See: https://docs.stripe.com/api/disputes/update
        """
        client = await self._get_client()

        evidence_data: dict[str, str] = {
            "evidence[uncategorized_text]": submission.evidence_text,
        }

        # Map our metadata to Stripe evidence fields
        meta = submission.metadata
        if meta.get("customer_name"):
            evidence_data["evidence[customer_name]"] = meta["customer_name"]
        if meta.get("receipt_text"):
            evidence_data["evidence[receipt]"] = meta["receipt_text"]
        if meta.get("service_date"):
            evidence_data["evidence[service_date]"] = meta["service_date"]

        # Submit evidence (does not close the dispute yet)
        evidence_data["submit"] = "true"

        try:
            resp = await client.post(
                f"/disputes/{submission.chargeback_id}",
                data=evidence_data,
                headers={"Idempotency-Key": f"dispute-{submission.dispute_id}"},
            )
            resp.raise_for_status()
            result = resp.json()

            return DisputeSubmissionResult(
                success=True,
                processor_dispute_id=result["id"],
                status=result.get("status", "submitted"),
                message="Evidence submitted successfully",
                raw_response={"id": result["id"], "status": result.get("status")},
            )
        except httpx.HTTPStatusError as e:
            error_body = e.response.json() if e.response.headers.get("content-type", "").startswith("application/json") else {}
            return DisputeSubmissionResult(
                success=False,
                status="failed",
                message=error_body.get("error", {}).get("message", str(e)),
                raw_response=error_body,
            )

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def get_dispute_status(self, processor_dispute_id: str) -> dict[str, Any]:
        """Check dispute status from Stripe."""
        client = await self._get_client()
        resp = await client.get(f"/disputes/{processor_dispute_id}")
        resp.raise_for_status()
        data = resp.json()
        return {
            "processor_dispute_id": data["id"],
            "status": data["status"],
            "reason": data.get("reason"),
            "amount": data.get("amount"),
            "created": data.get("created"),
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
