"""
Evidence collection service.
Gathers evidence from multiple sources for chargeback dispute response.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from app.core.observability import EVIDENCE_GENERATION_LATENCY
from app.integrations.payments.base import PaymentAdapter
from app.models.schemas import (
    ChargebackEvent,
    EvidenceItem,
    EvidenceStrength,
    FraudScoreResult,
    TransactionEvent,
)

logger = structlog.get_logger(__name__)


class EvidenceCollector:
    """
    Collects evidence from transaction data, payment processor records,
    and behavioral analysis to build a dispute response package.
    """

    def __init__(self, adapter: PaymentAdapter):
        self._adapter = adapter

    async def collect_evidence(
        self,
        chargeback: ChargebackEvent,
        transaction: TransactionEvent | None,
        fraud_score: FraudScoreResult | None,
    ) -> list[EvidenceItem]:
        """
        Collect all available evidence for a chargeback dispute.
        Returns a list of evidence items with strength assessments.
        """
        import time
        start = time.monotonic()

        items: list[EvidenceItem] = []

        # Transaction-level evidence
        if transaction:
            items.extend(self._extract_transaction_evidence(transaction))

        # Fraud score as evidence
        if fraud_score:
            items.extend(self._extract_fraud_evidence(fraud_score))

        # Payment processor evidence
        processor_evidence = await self._collect_processor_evidence(chargeback)
        items.extend(processor_evidence)

        # Order/restaurant-specific evidence
        if transaction and transaction.order_metadata:
            items.extend(self._extract_order_evidence(transaction))

        elapsed = time.monotonic() - start
        EVIDENCE_GENERATION_LATENCY.observe(elapsed)

        logger.info(
            "evidence.collected",
            chargeback_id=chargeback.chargeback_id,
            evidence_count=len(items),
            elapsed_s=round(elapsed, 3),
        )

        return items

    def assess_evidence_strength(self, items: list[EvidenceItem]) -> EvidenceStrength:
        """
        Assess overall evidence strength based on collected items.
        Uses a weighted scoring approach.
        """
        if not items:
            return EvidenceStrength.INSUFFICIENT

        weights = {
            EvidenceStrength.HIGH: 3,
            EvidenceStrength.MEDIUM: 2,
            EvidenceStrength.LOW: 1,
            EvidenceStrength.INSUFFICIENT: 0,
        }

        total_weight = sum(weights[item.strength] for item in items)
        max_possible = len(items) * 3

        ratio = total_weight / max_possible if max_possible > 0 else 0

        if ratio >= 0.7 and len(items) >= 3:
            return EvidenceStrength.HIGH
        elif ratio >= 0.4 and len(items) >= 2:
            return EvidenceStrength.MEDIUM
        elif ratio > 0:
            return EvidenceStrength.LOW
        else:
            return EvidenceStrength.INSUFFICIENT

    def _extract_transaction_evidence(self, txn: TransactionEvent) -> list[EvidenceItem]:
        """Evidence derived from the original transaction."""
        items: list[EvidenceItem] = []
        now = datetime.now(timezone.utc)

        # Card-present transactions are strong evidence against fraud claims
        if txn.transaction_type.value in ("card_present", "contactless"):
            items.append(EvidenceItem(
                evidence_type="card_present_verification",
                description="Transaction was conducted with physical card present (chip/contactless).",
                content=f"Transaction type: {txn.transaction_type.value}",
                strength=EvidenceStrength.HIGH,
                source="system",
                collected_at=now,
            ))

        # Device signals
        if txn.device_signals:
            ds = txn.device_signals
            if ds.geo_country and ds.geo_city:
                items.append(EvidenceItem(
                    evidence_type="geolocation_match",
                    description=f"Transaction originated from {ds.geo_city}, {ds.geo_country}.",
                    content=f"Location: {ds.geo_city}, {ds.geo_country}",
                    strength=EvidenceStrength.MEDIUM,
                    source="system",
                    collected_at=now,
                ))

        return items

    def _extract_fraud_evidence(self, score: FraudScoreResult) -> list[EvidenceItem]:
        """Evidence from fraud analysis."""
        now = datetime.now(timezone.utc)
        items: list[EvidenceItem] = []

        if score.fraud_score < 0.3:
            items.append(EvidenceItem(
                evidence_type="low_fraud_risk_assessment",
                description=f"Transaction scored {score.fraud_score:.2f} on fraud risk (low risk).",
                content=f"Model: {score.model_version}, Score: {score.fraud_score:.4f}",
                strength=EvidenceStrength.MEDIUM,
                source="system",
                collected_at=now,
            ))

        if not score.behavioral_anomaly_flags:
            items.append(EvidenceItem(
                evidence_type="no_behavioral_anomalies",
                description="No behavioral anomalies detected in transaction pattern.",
                strength=EvidenceStrength.MEDIUM,
                source="system",
                collected_at=now,
            ))

        return items

    async def _collect_processor_evidence(self, chargeback: ChargebackEvent) -> list[EvidenceItem]:
        """Evidence from the payment processor (AVS, 3DS, etc.)."""
        items: list[EvidenceItem] = []
        now = datetime.now(timezone.utc)

        try:
            txn_details = await self._adapter.fetch_transaction(chargeback.transaction_id)
            if txn_details and txn_details.raw_metadata:
                meta = txn_details.raw_metadata

                # AVS match
                avs_code = meta.get("avs_check") or meta.get("address_verification")
                if avs_code in ("Y", "A", "M", "pass"):
                    items.append(EvidenceItem(
                        evidence_type="avs_match",
                        description=f"Address Verification Service confirmed match (code: {avs_code}).",
                        strength=EvidenceStrength.HIGH,
                        source="payment_processor",
                        collected_at=now,
                    ))

                # CVV match
                cvv_check = meta.get("cvc_check") or meta.get("cvv_result")
                if cvv_check in ("pass", "match", "M"):
                    items.append(EvidenceItem(
                        evidence_type="cvv_match",
                        description="CVV/CVC verification passed.",
                        strength=EvidenceStrength.HIGH,
                        source="payment_processor",
                        collected_at=now,
                    ))

                # 3D Secure
                three_ds = meta.get("three_d_secure") or meta.get("3ds_result")
                if three_ds and isinstance(three_ds, dict) and three_ds.get("authenticated"):
                    items.append(EvidenceItem(
                        evidence_type="3ds_authenticated",
                        description="3D Secure authentication was successfully completed.",
                        strength=EvidenceStrength.HIGH,
                        source="payment_processor",
                        collected_at=now,
                    ))

        except Exception as e:
            logger.warning("evidence.processor_fetch_error", error=str(e))

        return items

    def _extract_order_evidence(self, txn: TransactionEvent) -> list[EvidenceItem]:
        """Restaurant-specific evidence from order data."""
        items: list[EvidenceItem] = []
        om = txn.order_metadata
        if om is None:
            return items

        now = datetime.now(timezone.utc)

        # Tip presence suggests legitimate cardholder
        if om.has_tip and om.tip_percentage > 0:
            items.append(EvidenceItem(
                evidence_type="tip_present",
                description=f"Customer left a {om.tip_percentage:.1f}% tip, indicating cardholder awareness.",
                content=f"Tip: {om.tip_percentage}%",
                strength=EvidenceStrength.MEDIUM,
                source="merchant",
                collected_at=now,
            ))

        # Order details as receipt proxy
        if om.item_count > 0:
            items.append(EvidenceItem(
                evidence_type="order_receipt",
                description=f"Order contained {om.item_count} item(s) via {om.order_channel} channel.",
                content=f"Items: {om.item_count}, Channel: {om.order_channel}",
                strength=EvidenceStrength.LOW,
                source="merchant",
                collected_at=now,
            ))

        return items
