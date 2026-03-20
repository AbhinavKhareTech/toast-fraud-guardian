"""
Square payment adapter.
Uses Square's documented Disputes API for evidence submission.
Docs: https://developer.squareup.com/docs/disputes-api/overview
"""

from __future__ import annotations

import hashlib
import hmac
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

_SQUARE_API_BASE = "https://connect.squareup.com/v2"
_SQUARE_SANDBOX_BASE = "https://connect.squareupsandbox.com/v2"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, PaymentAdapterError):
        return exc.retryable
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


class SquareAdapter(PaymentAdapter):

    def __init__(
        self,
        access_token: str | None = None,
        webhook_signature_key: str | None = None,
        sandbox: bool = False,
    ):
        settings = get_settings()
        self._access_token = access_token or settings.square_access_token.get_secret_value()
        self._webhook_key = webhook_signature_key or settings.square_webhook_signature_key.get_secret_value()
        self._base_url = _SQUARE_SANDBOX_BASE if sandbox else _SQUARE_API_BASE
        self._client: httpx.AsyncClient | None = None

    @property
    def processor_name(self) -> str:
        return "square"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Square-Version": "2024-06-04",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> bool:
        """Verify Square webhook notification signature."""
        signature = headers.get("x-square-hmacsha256-signature", "")
        if not signature or not self._webhook_key:
            return False

        # Square uses: HMAC-SHA256(webhook_signature_key, notification_url + body)
        # Since we may not know the notification_url at verification time,
        # we verify against body only (simplified; production should include URL)
        expected = hmac.new(
            self._webhook_key.encode(), payload, hashlib.sha256
        ).digest()
        import base64
        expected_b64 = base64.b64encode(expected).decode()
        return hmac.compare_digest(expected_b64, signature)

    async def parse_chargeback_webhook(
        self, payload: dict[str, Any]
    ) -> PaymentTransaction | None:
        """Parse Square dispute.created webhook."""
        event_type = payload.get("type", "")
        if "dispute" not in event_type:
            return None

        dispute = payload.get("data", {}).get("object", {}).get("dispute", {})
        payment_id = dispute.get("disputed_payment", {}).get("payment_id")

        if not payment_id:
            logger.warning("square.webhook.missing_payment_id", event=event_type)
            return None

        return await self.fetch_transaction(payment_id)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def fetch_transaction(self, transaction_id: str) -> PaymentTransaction | None:
        """Fetch payment details from Square Payments API."""
        client = await self._get_client()
        try:
            resp = await client.get(f"/payments/{transaction_id}")
            resp.raise_for_status()
            payment = resp.json().get("payment", {})

            return PaymentTransaction(
                transaction_id=payment["id"],
                merchant_id=payment.get("location_id", "unknown"),
                card_token=payment.get("card_details", {}).get("card", {}).get("fingerprint", f"card_{payment['id']}"),
                amount_cents=payment.get("amount_money", {}).get("amount", 0),
                currency=payment.get("amount_money", {}).get("currency", "USD"),
                transaction_type="card_present" if payment.get("card_details", {}).get("entry_method") == "EMV" else "card_not_present",
                timestamp=datetime.fromisoformat(payment["created_at"].replace("Z", "+00:00")),
                processor="square",
                raw_metadata={},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise PaymentAdapterError(
                f"Failed to fetch payment: {e.response.status_code}",
                processor="square",
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
        Submit dispute evidence via Square Disputes API.
        See: https://developer.squareup.com/docs/disputes-api/process-disputes
        """
        client = await self._get_client()

        # Step 1: Create evidence text
        try:
            create_resp = await client.post(
                f"/disputes/{submission.chargeback_id}/evidence-text",
                json={
                    "idempotency_key": f"evidence-{submission.dispute_id}",
                    "evidence_type": "REBUTTAL_EXPLANATION",
                    "evidence_text": submission.evidence_text,
                },
            )
            create_resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return DisputeSubmissionResult(
                success=False,
                status="failed",
                message=f"Evidence creation failed: {e.response.status_code}",
            )

        # Step 2: Submit the dispute
        try:
            submit_resp = await client.post(
                f"/disputes/{submission.chargeback_id}/submit-evidence",
                json={},
            )
            submit_resp.raise_for_status()
            result = submit_resp.json()

            return DisputeSubmissionResult(
                success=True,
                processor_dispute_id=submission.chargeback_id,
                status=result.get("dispute", {}).get("state", "submitted"),
                message="Evidence submitted successfully",
                raw_response=result,
            )
        except httpx.HTTPStatusError as e:
            return DisputeSubmissionResult(
                success=False,
                status="failed",
                message=f"Evidence submission failed: {e.response.status_code}",
            )

    async def get_dispute_status(self, processor_dispute_id: str) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(f"/disputes/{processor_dispute_id}")
        resp.raise_for_status()
        dispute = resp.json().get("dispute", {})
        return {
            "processor_dispute_id": dispute["id"],
            "status": dispute.get("state"),
            "reason": dispute.get("reason"),
            "amount": dispute.get("amount_money", {}).get("amount"),
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
