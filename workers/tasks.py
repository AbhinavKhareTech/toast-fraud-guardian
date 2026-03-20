"""
Celery tasks for async dispute processing.
Handles: dispute workflow execution, evidence submission, model retraining triggers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from celery import Celery

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

settings = get_settings()

celery_app = Celery(
    "toast_fraud_guardian",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=120,
    task_time_limit=180,
    task_default_queue="disputes",
    task_routes={
        "workers.tasks.process_dispute_task": {"queue": "disputes"},
        "workers.tasks.submit_dispute_task": {"queue": "submissions"},
        "workers.tasks.retrain_model_task": {"queue": "training"},
    },
)


def _run_async(coro):
    """Run an async function from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="workers.tasks.process_dispute_task",
)
def process_dispute_task(self, dispute_id: str, chargeback_data: dict[str, Any]) -> dict[str, Any]:
    """
    Execute the full dispute agent workflow.
    Called after webhook ingestion creates a dispute record.
    """
    logger.info("task.process_dispute.start", dispute_id=dispute_id)

    try:
        from app.agents.dispute_workflow import run_dispute_workflow
        from app.models.schemas import ChargebackEvent

        chargeback = ChargebackEvent(**chargeback_data)
        final_state = _run_async(run_dispute_workflow(dispute_id, chargeback))

        # Persist final state to DB
        _run_async(_persist_workflow_result(dispute_id, final_state))

        logger.info(
            "task.process_dispute.completed",
            dispute_id=dispute_id,
            decision=final_state.get("decision"),
        )

        return {
            "dispute_id": dispute_id,
            "decision": final_state.get("decision"),
            "status": "completed",
        }

    except Exception as exc:
        logger.error("task.process_dispute.failed", dispute_id=dispute_id, error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="workers.tasks.submit_dispute_task",
)
def submit_dispute_task(self, dispute_id: str) -> dict[str, Any]:
    """
    Submit dispute evidence to the payment processor.
    Called after human review approval or auto-submit decision.
    """
    logger.info("task.submit_dispute.start", dispute_id=dispute_id)

    try:
        result = _run_async(_submit_dispute(dispute_id))
        logger.info("task.submit_dispute.completed", dispute_id=dispute_id, success=result)
        return {"dispute_id": dispute_id, "submitted": result}

    except Exception as exc:
        logger.error("task.submit_dispute.failed", dispute_id=dispute_id, error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(name="workers.tasks.retrain_model_task")
def retrain_model_task(config: dict[str, Any] | None = None) -> dict[str, str]:
    """
    Trigger model retraining using accumulated feedback data.
    Typically run on a schedule (weekly) or when feedback volume threshold is met.
    """
    logger.info("task.retrain_model.start", config=config)
    # TODO: Implement training pipeline
    # 1. Export labeled data from fraud_score_log (where actual_fraud is not null)
    # 2. Run training script (ml/training/train.py)
    # 3. Export to ONNX (ml/export/to_onnx.py)
    # 4. Register new model version
    # 5. Hot-swap in scoring engine
    return {"status": "not_implemented", "message": "Training pipeline pending"}


# --- Helper Coroutines ---

async def _persist_workflow_result(dispute_id: str, state: dict[str, Any]) -> None:
    """Persist the full workflow result to the dispute record."""
    from app.core.database import get_db_session
    from app.core.security import generate_request_id
    from app.models.schemas import EvidenceStrength, FraudScoreResult
    from app.services.dispute_service import DisputeService

    trace_id = generate_request_id()

    async with get_db_session() as session:
        svc = DisputeService(session)

        if state.get("fraud_score_result"):
            score = FraudScoreResult(**state["fraud_score_result"])
            await svc.update_fraud_score(dispute_id, score, trace_id)

        if state.get("evidence_items"):
            from app.models.schemas import EvidenceItem
            items = [EvidenceItem(**item) for item in state["evidence_items"]]
            strength = EvidenceStrength(state.get("evidence_strength", "insufficient"))
            await svc.update_evidence(dispute_id, items, strength, trace_id)

        if state.get("dispute_letter"):
            await svc.update_dispute_letter(dispute_id, state["dispute_letter"], trace_id)

        if state.get("decision"):
            await svc.set_decision(
                dispute_id,
                decision=state["decision"],
                rationale=state.get("decision_rationale", ""),
                trace_id=trace_id,
            )


async def _submit_dispute(dispute_id: str) -> bool:
    """Submit a dispute that has been approved for submission."""
    from app.core.database import get_db_session
    from app.integrations.payments import get_adapter
    from app.integrations.payments.base import DisputeSubmission
    from app.services.dispute_service import DisputeService

    async with get_db_session() as session:
        svc = DisputeService(session)
        record = await svc.get_dispute(dispute_id)

        if record is None:
            logger.error("submit.dispute_not_found", dispute_id=dispute_id)
            return False

        adapter = get_adapter(record.payment_processor)
        submission = DisputeSubmission(
            dispute_id=record.id,
            chargeback_id=record.chargeback_id,
            transaction_id=record.transaction_id,
            evidence_text=record.dispute_letter or "",
        )

        result = await adapter.submit_dispute_evidence(submission)
        return result.success
