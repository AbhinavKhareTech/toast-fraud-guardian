"""
Toast payment adapter.

IMPORTANT: Toast's payment/dispute API endpoints are not publicly documented
at the level needed for direct integration. This adapter provides:
1. A clean interface matching our PaymentAdapter contract
2. A mock implementation for testing
3. Clearly marked integration points for when Toast API access is available

TODO: When Toast API docs/partnership are available:
- Replace mock implementations with real HTTP calls
- Implement OAuth2 token management for Toast API
- Add Toast-specific webhook signature verification
- Map Toast reason codes to our ChargebackReasonCode enum
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from app.integrations.payments.base import (
    DisputeSubmission,
    DisputeSubmissionResult,
    PaymentAdapter,
    PaymentTransaction,
)

logger = structlog.get_logger(__name__)


class ToastAdapter(PaymentAdapter):
    """
    Toast payment processor adapter.

    Current status: Interface-first implementation with sandbox mock.
    Toast's payments API requires partner-level access. This adapter
    is structured to be production-ready once API credentials and
    documentation are available.

    Known Toast API patterns (from public docs):
    - OAuth2 authentication with client credentials
    - REST API with JSON payloads
    - Webhook notifications for payment events
    - Restaurant-GUID based merchant identification
    """

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        environment: str = "sandbox",
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._environment = environment
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

        if environment != "sandbox":
            logger.warning(
                "toast.adapter.production_not_implemented",
                msg="Toast production integration requires partner API access. "
                    "Using sandbox mock.",
            )

    @property
    def processor_name(self) -> str:
        return "toast"

    async def _ensure_auth(self) -> None:
        """
        TODO: Implement Toast OAuth2 token exchange.
        Toast uses client_credentials grant:
        POST https://ws-api.toasttab.com/authentication/v1/authentication/login
        Body: { "clientId": "...", "clientSecret": "...", "userAccessType": "TOAST_MACHINE_CLIENT" }
        """
        if self._environment == "sandbox":
            self._access_token = "sandbox_mock_token"
            return

        # TODO: Real token exchange when API access is available
        raise NotImplementedError(
            "Toast production OAuth not yet implemented. "
            "Requires partner API credentials."
        )

    async def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> bool:
        """
        TODO: Implement Toast webhook signature verification.
        Toast webhook verification details are partner-specific.
        Expected pattern: HMAC-SHA256 with shared secret.
        """
        if self._environment == "sandbox":
            # In sandbox, accept all webhooks for testing
            logger.debug("toast.webhook.sandbox_bypass")
            return True

        # TODO: Implement real signature verification
        logger.warning("toast.webhook.verification_not_implemented")
        return False

    async def parse_chargeback_webhook(
        self, payload: dict[str, Any]
    ) -> PaymentTransaction | None:
        """
        Parse Toast chargeback webhook.

        TODO: Map Toast webhook payload structure when docs available.
        Expected fields based on Toast's data model:
        - restaurantGuid: merchant identifier
        - orderGuid: order reference
        - paymentGuid: payment reference
        - amount: dispute amount
        - reasonCode: chargeback reason
        """
        if self._environment == "sandbox":
            return self._mock_parse_chargeback(payload)

        raise NotImplementedError("Toast webhook parsing requires partner API docs")

    async def fetch_transaction(self, transaction_id: str) -> PaymentTransaction | None:
        """
        Fetch transaction from Toast.

        TODO: Use Toast Orders API when available:
        GET https://ws-api.toasttab.com/orders/v2/orders/{orderGuid}
        Headers: Authorization: Bearer {token}, Toast-Restaurant-External-ID: {guid}
        """
        if self._environment == "sandbox":
            return self._mock_fetch_transaction(transaction_id)

        raise NotImplementedError("Toast transaction fetch requires partner API access")

    async def submit_dispute_evidence(
        self, submission: DisputeSubmission
    ) -> DisputeSubmissionResult:
        """
        Submit dispute evidence to Toast.

        TODO: Toast may handle disputes through their payment processor partner
        (likely a card processor like Worldpay/FIS). The actual submission
        endpoint depends on Toast's dispute management workflow, which is
        not publicly documented.
        """
        if self._environment == "sandbox":
            return self._mock_submit_evidence(submission)

        raise NotImplementedError("Toast dispute submission requires partner API access")

    async def get_dispute_status(self, processor_dispute_id: str) -> dict[str, Any]:
        """TODO: Implement when Toast dispute status endpoint is available."""
        if self._environment == "sandbox":
            return {
                "processor_dispute_id": processor_dispute_id,
                "status": "under_review",
                "reason": "mock_sandbox",
            }

        raise NotImplementedError("Toast dispute status requires partner API access")

    # --- Sandbox Mock Implementations ---

    def _mock_parse_chargeback(self, payload: dict[str, Any]) -> PaymentTransaction | None:
        """Mock parser for testing. Mirrors expected Toast payload structure."""
        return PaymentTransaction(
            transaction_id=payload.get("paymentGuid", f"toast_mock_{id(payload)}"),
            merchant_id=payload.get("restaurantGuid", "mock_restaurant_001"),
            card_token=f"tok_toast_mock_{payload.get('paymentGuid', 'unknown')[:8]}",
            amount_cents=int(payload.get("amount", 0) * 100),
            currency="USD",
            transaction_type="card_present",
            timestamp=datetime.now(timezone.utc),
            processor="toast",
            raw_metadata=payload,
        )

    def _mock_fetch_transaction(self, transaction_id: str) -> PaymentTransaction:
        """Mock transaction fetch for sandbox testing."""
        return PaymentTransaction(
            transaction_id=transaction_id,
            merchant_id="mock_restaurant_001",
            card_token=f"tok_toast_mock_{transaction_id[:8]}",
            amount_cents=4599,
            currency="USD",
            transaction_type="card_present",
            timestamp=datetime.now(timezone.utc),
            processor="toast",
            raw_metadata={"sandbox": True, "order_type": "dine_in"},
        )

    def _mock_submit_evidence(self, submission: DisputeSubmission) -> DisputeSubmissionResult:
        """Mock evidence submission for sandbox testing."""
        logger.info(
            "toast.sandbox.evidence_submitted",
            dispute_id=submission.dispute_id,
            evidence_length=len(submission.evidence_text),
        )
        return DisputeSubmissionResult(
            success=True,
            processor_dispute_id=f"toast_disp_{submission.dispute_id}",
            status="submitted",
            message="[SANDBOX] Evidence submitted to Toast mock",
        )
