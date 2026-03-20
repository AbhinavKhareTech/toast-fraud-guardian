"""
Real-time feature engineering for fraud scoring.
Extracts behavioral, temporal, and merchant-level features from Redis feature store
and transaction context. Designed for sub-50ms feature vector assembly.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import structlog

from app.core.redis_client import get_feature_store, get_sequence_cache
from app.models.schemas import TransactionEvent

logger = structlog.get_logger(__name__)

# Feature vector dimension must match model input
FEATURE_DIM = 64
SEQUENCE_LENGTH = 20  # Last N transactions for behavioral sequence


class FeatureVector:
    """Typed feature vector with named feature access and numpy export."""

    __slots__ = ("_data", "_names")

    def __init__(self) -> None:
        self._data: dict[str, float] = {}
        self._names: list[str] = []

    def set(self, name: str, value: float) -> None:
        if name not in self._data:
            self._names.append(name)
        self._data[name] = value

    def to_numpy(self) -> np.ndarray:
        """Export as fixed-dimension numpy array, zero-padded if needed."""
        arr = np.zeros(FEATURE_DIM, dtype=np.float32)
        for i, name in enumerate(self._names):
            if i >= FEATURE_DIM:
                break
            arr[i] = self._data[name]
        return arr

    def to_dict(self) -> dict[str, float]:
        return dict(self._data)

    def __len__(self) -> int:
        return len(self._data)


class SequenceFeatures:
    """Behavioral sequence data for the sequence encoder (GRU/Transformer)."""

    __slots__ = ("amounts", "time_deltas", "merchant_categories", "channels", "length")

    def __init__(self) -> None:
        self.amounts: list[float] = []
        self.time_deltas: list[float] = []
        self.merchant_categories: list[int] = []
        self.channels: list[int] = []
        self.length: int = 0

    def to_padded_array(self) -> np.ndarray:
        """Export as (SEQUENCE_LENGTH, 4) array, zero-padded."""
        arr = np.zeros((SEQUENCE_LENGTH, 4), dtype=np.float32)
        n = min(self.length, SEQUENCE_LENGTH)
        if n > 0:
            arr[:n, 0] = self.amounts[:n]
            arr[:n, 1] = self.time_deltas[:n]
            arr[:n, 2] = self.merchant_categories[:n]
            arr[:n, 3] = self.channels[:n]
        return arr


# Channel encoding map
_CHANNEL_MAP = {"in_store": 0, "online": 1, "app": 2, "phone": 3, "unknown": 4}


async def extract_features(txn: TransactionEvent) -> tuple[FeatureVector, SequenceFeatures]:
    """
    Extract full feature vector + behavioral sequence for a transaction.
    Combines real-time Redis lookups with transaction-level features.
    Target: < 50ms total extraction time.
    """
    start = time.monotonic()
    fv = FeatureVector()
    seq = SequenceFeatures()

    # --- Transaction-level features ---
    _extract_transaction_features(txn, fv)

    # --- Device/IP features ---
    if txn.device_signals:
        _extract_device_features(txn, fv)

    # --- Order metadata features (restaurant-specific) ---
    if txn.order_metadata:
        _extract_order_features(txn, fv)

    # --- Redis: Card profile features ---
    await _extract_card_profile_features(txn.card_token, txn.merchant_id, fv)

    # --- Redis: Merchant profile features ---
    await _extract_merchant_profile_features(txn.merchant_id, fv)

    # --- Redis: Behavioral sequence ---
    await _extract_behavioral_sequence(txn.card_token, seq)

    # --- Store current transaction in sequence cache for future lookups ---
    await _append_to_sequence_cache(txn)

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.debug("features.extracted", txn_id=txn.transaction_id, features=len(fv), elapsed_ms=round(elapsed_ms, 2))

    return fv, seq


def _extract_transaction_features(txn: TransactionEvent, fv: FeatureVector) -> None:
    """Core transaction attributes."""
    fv.set("amount_cents", float(txn.amount_cents))
    fv.set("amount_log", float(np.log1p(txn.amount_cents)))

    # Time-based features
    ts = txn.timestamp
    fv.set("hour_of_day", float(ts.hour))
    fv.set("day_of_week", float(ts.weekday()))
    fv.set("is_weekend", float(ts.weekday() >= 5))
    fv.set("is_late_night", float(ts.hour >= 22 or ts.hour <= 5))

    # Cyclical time encoding
    hour_rad = 2 * np.pi * ts.hour / 24
    fv.set("hour_sin", float(np.sin(hour_rad)))
    fv.set("hour_cos", float(np.cos(hour_rad)))

    dow_rad = 2 * np.pi * ts.weekday() / 7
    fv.set("dow_sin", float(np.sin(dow_rad)))
    fv.set("dow_cos", float(np.cos(dow_rad)))

    # Transaction type encoding
    type_map = {"card_present": 0, "card_not_present": 1, "contactless": 2, "mobile_wallet": 3, "online": 4}
    fv.set("txn_type", float(type_map.get(txn.transaction_type.value, 4)))
    fv.set("is_card_not_present", float(txn.transaction_type.value in ("card_not_present", "online")))


def _extract_device_features(txn: TransactionEvent, fv: FeatureVector) -> None:
    """Device and network risk signals."""
    ds = txn.device_signals
    if ds is None:
        return
    fv.set("is_known_proxy", float(ds.is_known_proxy))
    fv.set("is_tor_exit", float(ds.is_tor_exit))
    fv.set("has_device_fingerprint", float(ds.device_fingerprint is not None))
    fv.set("has_geo", float(ds.geo_lat is not None and ds.geo_lng is not None))


def _extract_order_features(txn: TransactionEvent, fv: FeatureVector) -> None:
    """Restaurant-specific order context."""
    om = txn.order_metadata
    if om is None:
        return
    fv.set("item_count", float(om.item_count))
    fv.set("avg_item_price_cents", float(om.avg_item_price_cents))
    fv.set("has_alcohol", float(om.has_alcohol))
    fv.set("has_tip", float(om.has_tip))
    fv.set("tip_percentage", om.tip_percentage)
    fv.set("order_channel", float(_CHANNEL_MAP.get(om.order_channel, 4)))

    # Anomaly signals
    if om.item_count > 0 and txn.amount_cents > 0:
        fv.set("price_per_item", float(txn.amount_cents / om.item_count))
    if om.time_to_complete_seconds is not None:
        fv.set("time_to_complete_s", float(om.time_to_complete_seconds))


async def _extract_card_profile_features(
    card_token: str, merchant_id: str, fv: FeatureVector
) -> None:
    """
    Card-level behavioral profile from Redis feature store.
    Keys: card_profile:{card_token_hash}
    Fields: txn_count_24h, txn_count_7d, avg_amount_7d, distinct_merchants_7d,
            last_txn_timestamp, velocity_1h
    """
    store = get_feature_store()
    key = f"card_profile:{_hash_token(card_token)}"
    try:
        profile = await store.hgetall(key)
        if profile:
            fv.set("card_txn_count_24h", float(profile.get("txn_count_24h", 0)))
            fv.set("card_txn_count_7d", float(profile.get("txn_count_7d", 0)))
            fv.set("card_avg_amount_7d", float(profile.get("avg_amount_7d", 0)))
            fv.set("card_distinct_merchants_7d", float(profile.get("distinct_merchants_7d", 0)))
            fv.set("card_velocity_1h", float(profile.get("velocity_1h", 0)))

            last_ts = profile.get("last_txn_timestamp")
            if last_ts:
                delta = (datetime.now(timezone.utc) - datetime.fromisoformat(last_ts)).total_seconds()
                fv.set("card_seconds_since_last_txn", float(delta))
        else:
            # First-time card: high risk signal
            fv.set("card_is_new", 1.0)
    except Exception as e:
        logger.warning("features.card_profile_error", error=str(e), card_hash=_hash_token(card_token)[:8])


async def _extract_merchant_profile_features(merchant_id: str, fv: FeatureVector) -> None:
    """
    Merchant-level aggregates from Redis.
    Keys: merchant_profile:{merchant_id}
    """
    store = get_feature_store()
    key = f"merchant_profile:{merchant_id}"
    try:
        profile = await store.hgetall(key)
        if profile:
            fv.set("merchant_avg_txn_amount", float(profile.get("avg_txn_amount", 0)))
            fv.set("merchant_chargeback_rate_30d", float(profile.get("chargeback_rate_30d", 0)))
            fv.set("merchant_txn_volume_24h", float(profile.get("txn_volume_24h", 0)))
            fv.set("merchant_fraud_rate_90d", float(profile.get("fraud_rate_90d", 0)))
    except Exception as e:
        logger.warning("features.merchant_profile_error", error=str(e), merchant_id=merchant_id)


async def _extract_behavioral_sequence(card_token: str, seq: SequenceFeatures) -> None:
    """
    Load recent transaction sequence for the card from Redis sorted set.
    Keys: card_seq:{card_token_hash} (sorted set, score=timestamp)
    Values: JSON-encoded mini transaction records
    """
    cache = get_sequence_cache()
    key = f"card_seq:{_hash_token(card_token)}"
    try:
        import orjson
        raw_items = await cache.zrevrange(key, 0, SEQUENCE_LENGTH - 1)
        if raw_items:
            prev_ts: float | None = None
            for raw in reversed(raw_items):  # oldest first
                item = orjson.loads(raw)
                seq.amounts.append(float(item.get("amount", 0)))
                ts = float(item.get("timestamp", 0))
                if prev_ts is not None:
                    seq.time_deltas.append(ts - prev_ts)
                else:
                    seq.time_deltas.append(0.0)
                prev_ts = ts
                seq.merchant_categories.append(int(item.get("mcc", 0)))
                seq.channels.append(int(item.get("channel", 0)))
            seq.length = len(seq.amounts)
    except Exception as e:
        logger.warning("features.sequence_error", error=str(e))


async def _append_to_sequence_cache(txn: TransactionEvent) -> None:
    """Append current transaction to the behavioral sequence cache."""
    cache = get_sequence_cache()
    key = f"card_seq:{_hash_token(txn.card_token)}"
    try:
        import orjson
        record = orjson.dumps({
            "amount": txn.amount_cents,
            "timestamp": txn.timestamp.timestamp(),
            "mcc": 5812,  # Restaurant MCC
            "channel": _CHANNEL_MAP.get(
                txn.order_metadata.order_channel if txn.order_metadata else "unknown", 4
            ),
        })
        score = txn.timestamp.timestamp()
        await cache.zadd(key, {record: score})
        # Trim to keep only recent transactions
        await cache.zremrangebyrank(key, 0, -(SEQUENCE_LENGTH + 1))
        # TTL: 30 days
        await cache.expire(key, 30 * 86400)
    except Exception as e:
        logger.warning("features.sequence_append_error", error=str(e))


def _hash_token(token: str) -> str:
    """Hash a card token for Redis key usage (privacy-safe)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]
