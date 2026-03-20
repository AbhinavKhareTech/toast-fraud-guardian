"""
Admin API: model versioning, feature flags, GDPR deletion, system health.
Separated from main API for access control purposes.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Body, HTTPException

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.security import generate_request_id

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# --- Model Versioning ---

@router.get("/models")
async def list_model_versions() -> dict[str, Any]:
    """List all registered model versions."""
    from sqlalchemy import select
    from app.models.orm import ModelVersion

    async with get_db_session() as session:
        result = await session.execute(
            select(ModelVersion).order_by(ModelVersion.deployed_at.desc())
        )
        versions = result.scalars().all()

    return {
        "models": [
            {
                "version": v.version,
                "model_path": v.model_path,
                "is_active": v.is_active,
                "metrics": v.metrics,
                "deployed_at": v.deployed_at.isoformat() if v.deployed_at else None,
            }
            for v in versions
        ]
    }


@router.post("/models/register")
async def register_model_version(
    version: str = Body(..., embed=True),
    model_path: str = Body(..., embed=True),
    metrics: dict[str, Any] | None = Body(None, embed=True),
    training_config: dict[str, Any] | None = Body(None, embed=True),
) -> dict[str, str]:
    """Register a new model version (does not activate it)."""
    from app.models.orm import ModelVersion

    async with get_db_session() as session:
        mv = ModelVersion(
            version=version,
            model_path=model_path,
            metrics=metrics,
            is_active=False,
            training_config=training_config,
        )
        session.add(mv)

    logger.info("admin.model.registered", version=version, path=model_path)
    return {"status": "registered", "version": version}


@router.post("/models/{version}/activate")
async def activate_model_version(version: str) -> dict[str, str]:
    """
    Activate a model version for production scoring.
    Deactivates all other versions and hot-swaps the ONNX model.
    """
    from sqlalchemy import select, update
    from app.models.orm import ModelVersion

    async with get_db_session() as session:
        # Verify version exists
        result = await session.execute(
            select(ModelVersion).where(ModelVersion.version == version)
        )
        mv = result.scalar_one_or_none()
        if mv is None:
            raise HTTPException(status_code=404, detail=f"Model version {version} not found")

        # Deactivate all, activate target
        await session.execute(update(ModelVersion).values(is_active=False))
        await session.execute(
            update(ModelVersion)
            .where(ModelVersion.version == version)
            .values(is_active=True)
        )

    # Hot-swap in scoring engine
    try:
        from ml.inference.scoring_engine import get_scoring_engine
        engine = get_scoring_engine()
        engine.load_model(mv.model_path, version)
        logger.info("admin.model.activated", version=version)
    except Exception as e:
        logger.error("admin.model.activation_failed", version=version, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to load model: {e}")

    return {"status": "activated", "version": version}


# --- Feature Flags ---

@router.get("/feature-flags")
async def get_feature_flags() -> dict[str, bool]:
    """Get current feature flag states."""
    settings = get_settings()
    return {
        "ff_auto_submit_disputes": settings.ff_auto_submit_disputes,
        "ff_sequence_model_enabled": settings.ff_sequence_model_enabled,
        "ff_llm_evidence_writer": settings.ff_llm_evidence_writer,
        "ff_ab_test_scoring": settings.ff_ab_test_scoring,
    }


# --- GDPR Deletion ---

@router.post("/gdpr/delete")
async def request_deletion(
    entity_type: str = Body(..., embed=True),
    entity_id: str = Body(..., embed=True),
    requested_by: str = Body(..., embed=True),
) -> dict[str, str]:
    """
    Submit a GDPR/CCPA right-to-delete request.
    Queues an async task to scrub all PII for the given entity.
    """
    if entity_type not in ("merchant", "card_token"):
        raise HTTPException(status_code=400, detail="entity_type must be 'merchant' or 'card_token'")

    from workers.retention import process_deletion_request
    task = process_deletion_request.delay(entity_type, entity_id, requested_by)

    logger.info(
        "admin.gdpr.deletion_requested",
        entity_type=entity_type,
        entity_id=entity_id[:8] + "...",
        requested_by=requested_by,
        task_id=task.id,
    )

    return {"status": "queued", "task_id": task.id}


# --- System Info ---

@router.get("/system/config")
async def get_system_config() -> dict[str, Any]:
    """Get non-secret system configuration for debugging."""
    settings = get_settings()
    return {
        "app_env": settings.app_env.value,
        "model_version": settings.model_version,
        "fraud_score_threshold_auto": settings.fraud_score_threshold_auto,
        "fraud_score_threshold_review": settings.fraud_score_threshold_review,
        "pii_retention_days": settings.pii_retention_days,
        "audit_log_retention_days": settings.audit_log_retention_days,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
    }
