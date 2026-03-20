"""
ONNX Runtime inference engine for fraud scoring.
Provides sub-200ms scoring with batching support and model versioning.
Falls back to a heuristic scorer if ONNX model is not available.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from app.core.config import get_settings
from app.core.observability import (
    FRAUD_SCORE_DISTRIBUTION,
    FRAUD_SCORE_LATENCY,
    MODEL_INFERENCE_ERRORS,
    TRANSACTIONS_SCORED,
)
from app.models.schemas import (
    FraudDecision,
    FraudScoreResult,
    TransactionEvent,
)
from ml.inference.features import FeatureVector, SequenceFeatures, extract_features

logger = structlog.get_logger(__name__)


class FraudScoringEngine:
    """
    Production fraud scoring engine backed by ONNX Runtime.
    Thread-safe, supports model hot-swapping via version parameter.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}  # version -> ort.InferenceSession
        self._active_version: str | None = None
        self._settings = get_settings()

    def load_model(self, model_path: str, version: str) -> None:
        """Load an ONNX model into memory."""
        import onnxruntime as ort

        if not Path(model_path).exists():
            logger.warning("scorer.model_not_found", path=model_path, version=version)
            return

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.enable_mem_pattern = True

        session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        self._sessions[version] = session
        self._active_version = version
        logger.info("scorer.model_loaded", version=version, path=model_path)

    def load_default(self) -> None:
        """Load the configured default model."""
        self.load_model(
            self._settings.onnx_model_path,
            self._settings.model_version,
        )

    @property
    def is_ready(self) -> bool:
        return self._active_version is not None and self._active_version in self._sessions

    async def score_transaction(
        self,
        txn: TransactionEvent,
        version: str | None = None,
    ) -> FraudScoreResult:
        """
        Score a single transaction. Returns fraud score with decision.
        Full pipeline: feature extraction -> inference -> decision.
        """
        start = time.monotonic()
        model_version = version or self._active_version or self._settings.model_version

        try:
            # Feature extraction (async, hits Redis)
            feature_vector, sequence_features = await extract_features(txn)

            # Inference
            if self.is_ready and model_version in self._sessions:
                fraud_score = self._run_onnx_inference(
                    feature_vector, sequence_features, model_version
                )
            else:
                # Fallback: heuristic scoring when model is not loaded
                fraud_score = self._heuristic_score(feature_vector, txn)
                model_version = f"{model_version}-heuristic"

            # Decision
            decision = self._make_decision(fraud_score)

            elapsed_ms = (time.monotonic() - start) * 1000

            # Metrics
            FRAUD_SCORE_LATENCY.observe(elapsed_ms / 1000)
            FRAUD_SCORE_DISTRIBUTION.observe(fraud_score)
            TRANSACTIONS_SCORED.labels(decision=decision.value).inc()

            # Anomaly flags
            anomaly_flags = self._detect_anomalies(feature_vector, txn)

            result = FraudScoreResult(
                transaction_id=txn.transaction_id,
                merchant_id=txn.merchant_id,
                fraud_score=round(fraud_score, 6),
                decision=decision,
                model_version=model_version,
                feature_contributions=self._top_contributions(feature_vector, fraud_score),
                sequence_risk_score=round(fraud_score * 0.4, 4),  # Approximate sequence contribution
                behavioral_anomaly_flags=anomaly_flags,
                scored_at=datetime.now(timezone.utc),
                latency_ms=round(elapsed_ms, 2),
            )

            logger.info(
                "scorer.scored",
                txn_id=txn.transaction_id,
                score=result.fraud_score,
                decision=decision.value,
                latency_ms=result.latency_ms,
                model=model_version,
            )

            return result

        except Exception as e:
            MODEL_INFERENCE_ERRORS.labels(error_type=type(e).__name__).inc()
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.error("scorer.error", txn_id=txn.transaction_id, error=str(e))

            # Fail-open with high review score
            return FraudScoreResult(
                transaction_id=txn.transaction_id,
                merchant_id=txn.merchant_id,
                fraud_score=0.5,
                decision=FraudDecision.REVIEW,
                model_version=f"{model_version}-fallback",
                scored_at=datetime.now(timezone.utc),
                latency_ms=round(elapsed_ms, 2),
                behavioral_anomaly_flags=["inference_error"],
            )

    def _run_onnx_inference(
        self,
        fv: FeatureVector,
        seq: SequenceFeatures,
        version: str,
    ) -> float:
        """Run ONNX model inference. Must be < 50ms."""
        session = self._sessions[version]

        features_np = fv.to_numpy().reshape(1, -1)
        sequence_np = seq.to_padded_array().reshape(1, seq.to_padded_array().shape[0], -1)
        lengths_np = np.array([seq.length], dtype=np.int64)

        result = session.run(
            None,
            {
                "features": features_np,
                "sequence": sequence_np,
                "seq_lengths": lengths_np,
            },
        )

        return float(np.clip(result[0][0][0], 0.0, 1.0))

    def _heuristic_score(self, fv: FeatureVector, txn: TransactionEvent) -> float:
        """
        Rule-based fallback scorer for when the ML model is unavailable.
        Uses interpretable risk factors with weighted scoring.
        """
        score = 0.1  # Base risk

        features = fv.to_dict()

        # High-value transactions
        amount = txn.amount_cents
        if amount > 50000:  # > $500
            score += 0.15
        elif amount > 20000:  # > $200
            score += 0.05

        # Card-not-present
        if features.get("is_card_not_present", 0) > 0:
            score += 0.1

        # Late night
        if features.get("is_late_night", 0) > 0:
            score += 0.05

        # Proxy/Tor
        if features.get("is_known_proxy", 0) > 0:
            score += 0.15
        if features.get("is_tor_exit", 0) > 0:
            score += 0.2

        # New card
        if features.get("card_is_new", 0) > 0:
            score += 0.1

        # High velocity
        velocity = features.get("card_velocity_1h", 0)
        if velocity > 5:
            score += 0.2
        elif velocity > 3:
            score += 0.1

        # Merchant chargeback history
        cb_rate = features.get("merchant_chargeback_rate_30d", 0)
        if cb_rate > 0.02:
            score += 0.1

        return min(score, 1.0)

    def _make_decision(self, fraud_score: float) -> FraudDecision:
        """Apply threshold-based decision logic."""
        if fraud_score >= self._settings.fraud_score_threshold_auto:
            return FraudDecision.DECLINE
        elif fraud_score >= self._settings.fraud_score_threshold_review:
            return FraudDecision.REVIEW
        else:
            return FraudDecision.APPROVE

    def _detect_anomalies(self, fv: FeatureVector, txn: TransactionEvent) -> list[str]:
        """Flag behavioral anomalies for human review context."""
        flags: list[str] = []
        features = fv.to_dict()

        if features.get("card_velocity_1h", 0) > 5:
            flags.append("high_velocity")
        if features.get("is_tor_exit", 0) > 0:
            flags.append("tor_exit_node")
        if features.get("is_known_proxy", 0) > 0:
            flags.append("known_proxy")
        if features.get("card_is_new", 0) > 0:
            flags.append("first_seen_card")
        if txn.amount_cents > 50000:
            flags.append("high_value_transaction")
        if features.get("is_late_night", 0) > 0 and features.get("is_card_not_present", 0) > 0:
            flags.append("late_night_cnp")

        return flags

    def _top_contributions(
        self, fv: FeatureVector, score: float, top_k: int = 5
    ) -> dict[str, float]:
        """
        Approximate feature contributions for explainability.
        For production, consider SHAP values computed offline.
        """
        features = fv.to_dict()
        if not features:
            return {}

        # Simple absolute-value ranking as proxy for contribution
        sorted_feats = sorted(features.items(), key=lambda x: abs(x[1]), reverse=True)
        return {k: round(v, 4) for k, v in sorted_feats[:top_k]}


# Module-level singleton
_engine: FraudScoringEngine | None = None


def get_scoring_engine() -> FraudScoringEngine:
    global _engine
    if _engine is None:
        _engine = FraudScoringEngine()
        try:
            _engine.load_default()
        except Exception as e:
            logger.warning("scorer.default_load_failed", error=str(e))
    return _engine
