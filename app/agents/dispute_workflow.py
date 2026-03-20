"""
LangGraph multi-agent workflow for chargeback dispute automation.

Workflow:
    ┌─────────────────┐
    │ transaction_     │
    │ scorer           │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │ evidence_        │
    │ collector        │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │ evidence_        │
    │ writer (LLM)     │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │ decision_        │
    │ engine           │
    └────────┬────────┘
             │
      ┌──────┴──────┐
      │             │
  auto_submit   human_review
      │             │
    ┌─▼──────────┐  └──▶ queue
    │ dispute_   │
    │ submitter  │
    └────────────┘

Each node operates on AgentWorkflowState and is independently testable.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, TypedDict

import orjson
import structlog
from langgraph.graph import END, StateGraph

from app.core.config import get_settings
from app.core.observability import AGENT_WORKFLOW_LATENCY
from app.integrations.payments import get_adapter
from app.models.schemas import (
    AgentWorkflowState,
    ChargebackEvent,
    EvidenceStrength,
    TransactionEvent,
)
from app.services.evidence_service import EvidenceCollector
from app.services.llm_service import (
    DISPUTE_LETTER_SYSTEM_PROMPT,
    DISPUTE_LETTER_USER_TEMPLATE,
    EVIDENCE_SUMMARY_SYSTEM_PROMPT,
    EVIDENCE_SUMMARY_USER_TEMPLATE,
    get_llm_client,
)
from ml.inference.scoring_engine import get_scoring_engine

logger = structlog.get_logger(__name__)


# --- LangGraph State Type ---

class WorkflowState(TypedDict, total=False):
    """State passed through the LangGraph workflow."""
    dispute_id: str
    chargeback_event: dict[str, Any]
    transaction: dict[str, Any] | None
    fraud_score_result: dict[str, Any] | None
    evidence_items: list[dict[str, Any]]
    evidence_strength: str | None
    dispute_letter: str | None
    decision: str | None
    decision_rationale: str | None
    errors: list[str]
    current_step: str
    started_at: str | None
    completed_at: str | None


# --- Node Functions ---

async def transaction_scorer_node(state: WorkflowState) -> WorkflowState:
    """Score the original transaction for fraud risk."""
    logger.info("agent.node.transaction_scorer", dispute_id=state["dispute_id"])

    try:
        chargeback = ChargebackEvent(**state["chargeback_event"])

        # Fetch transaction from payment processor
        adapter = get_adapter(chargeback.payment_processor)
        txn_data = await adapter.fetch_transaction(chargeback.transaction_id)

        if txn_data is None:
            state["errors"] = state.get("errors", []) + ["Transaction not found in processor"]
            state["current_step"] = "transaction_scorer_failed"
            return state

        # Build TransactionEvent from processor data
        txn = TransactionEvent(
            transaction_id=txn_data.transaction_id,
            merchant_id=txn_data.merchant_id,
            card_token=txn_data.card_token,
            amount_cents=txn_data.amount_cents,
            currency=txn_data.currency,
            transaction_type=txn_data.transaction_type,
            timestamp=txn_data.timestamp,
            payment_processor=txn_data.processor,
        )

        # Score
        engine = get_scoring_engine()
        score_result = await engine.score_transaction(txn)

        state["transaction"] = txn.model_dump(mode="json")
        state["fraud_score_result"] = score_result.model_dump(mode="json")
        state["current_step"] = "scored"

    except Exception as e:
        logger.error("agent.scorer.error", error=str(e), dispute_id=state["dispute_id"])
        state["errors"] = state.get("errors", []) + [f"Scoring error: {str(e)}"]
        state["current_step"] = "scorer_error"

    return state


async def evidence_collector_node(state: WorkflowState) -> WorkflowState:
    """Collect evidence from all available sources."""
    logger.info("agent.node.evidence_collector", dispute_id=state["dispute_id"])

    try:
        chargeback = ChargebackEvent(**state["chargeback_event"])
        adapter = get_adapter(chargeback.payment_processor)
        collector = EvidenceCollector(adapter)

        transaction = TransactionEvent(**state["transaction"]) if state.get("transaction") else None
        from app.models.schemas import FraudScoreResult
        fraud_score = FraudScoreResult(**state["fraud_score_result"]) if state.get("fraud_score_result") else None

        items = await collector.collect_evidence(chargeback, transaction, fraud_score)
        strength = collector.assess_evidence_strength(items)

        state["evidence_items"] = [item.model_dump(mode="json") for item in items]
        state["evidence_strength"] = strength.value
        state["current_step"] = "evidence_collected"

    except Exception as e:
        logger.error("agent.evidence.error", error=str(e), dispute_id=state["dispute_id"])
        state["errors"] = state.get("errors", []) + [f"Evidence collection error: {str(e)}"]
        state["current_step"] = "evidence_error"

    return state


async def evidence_writer_node(state: WorkflowState) -> WorkflowState:
    """Generate dispute letter using LLM."""
    logger.info("agent.node.evidence_writer", dispute_id=state["dispute_id"])

    settings = get_settings()
    if not settings.ff_llm_evidence_writer:
        state["dispute_letter"] = _generate_template_letter(state)
        state["current_step"] = "letter_generated_template"
        return state

    try:
        chargeback = ChargebackEvent(**state["chargeback_event"])
        fraud_score = state.get("fraud_score_result", {}).get("fraud_score", 0)

        # Format evidence for prompt
        evidence_text = "\n".join(
            f"- [{item.get('evidence_type')}] {item.get('description')} (Strength: {item.get('strength')})"
            for item in state.get("evidence_items", [])
        )

        behavioral_signals = ", ".join(
            state.get("fraud_score_result", {}).get("behavioral_anomaly_flags", [])
        ) or "None detected"

        user_prompt = DISPUTE_LETTER_USER_TEMPLATE.format(
            transaction_id=chargeback.transaction_id,
            merchant_name=f"Merchant {chargeback.merchant_id}",
            merchant_id=chargeback.merchant_id,
            amount=chargeback.amount_cents / 100,
            transaction_date=chargeback.received_at.strftime("%Y-%m-%d"),
            transaction_type=state.get("transaction", {}).get("transaction_type", "unknown"),
            reason_code=chargeback.reason_code,
            reason_description=chargeback.reason_description or "Not provided",
            evidence_summary=evidence_text,
            fraud_score=fraud_score,
            behavioral_signals=behavioral_signals,
        )

        llm = get_llm_client()
        response = await llm.generate(
            system_prompt=DISPUTE_LETTER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        state["dispute_letter"] = response.content
        state["current_step"] = "letter_generated"

        logger.info(
            "agent.writer.completed",
            dispute_id=state["dispute_id"],
            letter_length=len(response.content),
            tokens=response.usage_tokens,
        )

    except Exception as e:
        logger.error("agent.writer.error", error=str(e), dispute_id=state["dispute_id"])
        state["dispute_letter"] = _generate_template_letter(state)
        state["errors"] = state.get("errors", []) + [f"LLM error (fell back to template): {str(e)}"]
        state["current_step"] = "letter_generated_fallback"

    return state


async def decision_engine_node(state: WorkflowState) -> WorkflowState:
    """
    Make the automation decision.
    IF fraud_score > 0.85 AND evidence_strength == HIGH -> auto_submit
    ELSE -> human_review
    """
    logger.info("agent.node.decision_engine", dispute_id=state["dispute_id"])

    settings = get_settings()
    fraud_score = state.get("fraud_score_result", {}).get("fraud_score", 0.5)
    evidence_strength = state.get("evidence_strength", "insufficient")
    has_letter = bool(state.get("dispute_letter"))
    has_errors = bool(state.get("errors"))

    # Decision logic
    # Note: For chargebacks, a HIGH fraud score means the txn WAS likely fraud,
    # so we want to DECLINE the dispute (merchant loses). Low score = legitimate txn = fight dispute.
    is_legitimate_txn = fraud_score < (1 - settings.fraud_score_threshold_auto)  # Low fraud = legitimate
    strong_evidence = evidence_strength in ("high", "medium")
    auto_submit_enabled = settings.ff_auto_submit_disputes

    if is_legitimate_txn and evidence_strength == "high" and has_letter and not has_errors and auto_submit_enabled:
        decision = "auto_submit"
        rationale = (
            f"Auto-submitting: low fraud risk ({fraud_score:.3f}), "
            f"strong evidence ({evidence_strength}), letter generated."
        )
    elif has_errors or evidence_strength in ("insufficient", "low"):
        decision = "human_review"
        rationale = (
            f"Requires review: errors={has_errors}, "
            f"evidence_strength={evidence_strength}, fraud_score={fraud_score:.3f}."
        )
    elif not auto_submit_enabled:
        decision = "human_review"
        rationale = "Auto-submission disabled by feature flag."
    else:
        decision = "human_review"
        rationale = f"Default to review: fraud_score={fraud_score:.3f}, evidence={evidence_strength}."

    state["decision"] = decision
    state["decision_rationale"] = rationale
    state["current_step"] = f"decided_{decision}"

    logger.info(
        "agent.decision",
        dispute_id=state["dispute_id"],
        decision=decision,
        fraud_score=fraud_score,
        evidence_strength=evidence_strength,
    )

    return state


async def dispute_submitter_node(state: WorkflowState) -> WorkflowState:
    """Submit dispute evidence to the payment processor."""
    logger.info("agent.node.dispute_submitter", dispute_id=state["dispute_id"])

    try:
        chargeback = ChargebackEvent(**state["chargeback_event"])
        adapter = get_adapter(chargeback.payment_processor)

        from app.integrations.payments.base import DisputeSubmission
        submission = DisputeSubmission(
            dispute_id=state["dispute_id"],
            chargeback_id=chargeback.chargeback_id,
            transaction_id=chargeback.transaction_id,
            evidence_text=state.get("dispute_letter", ""),
        )

        result = await adapter.submit_dispute_evidence(submission)

        if result.success:
            state["current_step"] = "submitted"
            logger.info("agent.submitter.success", dispute_id=state["dispute_id"])
        else:
            state["errors"] = state.get("errors", []) + [f"Submission failed: {result.message}"]
            state["current_step"] = "submission_failed"
            logger.error("agent.submitter.failed", message=result.message)

    except Exception as e:
        logger.error("agent.submitter.error", error=str(e))
        state["errors"] = state.get("errors", []) + [f"Submission error: {str(e)}"]
        state["current_step"] = "submission_error"

    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    return state


# --- Routing ---

def route_after_decision(state: WorkflowState) -> str:
    """Route based on decision: auto_submit goes to submitter, else ends."""
    if state.get("decision") == "auto_submit":
        return "dispute_submitter"
    return END


# --- Template Fallback ---

def _generate_template_letter(state: WorkflowState) -> str:
    """Simple template-based dispute letter when LLM is unavailable."""
    chargeback_data = state.get("chargeback_event", {})
    evidence_items = state.get("evidence_items", [])

    evidence_lines = "\n".join(
        f"  - {item.get('description', 'N/A')}"
        for item in evidence_items
    )

    return f"""To Whom It May Concern,

We are writing to dispute chargeback {chargeback_data.get('chargeback_id', 'N/A')} \
for transaction {chargeback_data.get('transaction_id', 'N/A')} \
in the amount of ${chargeback_data.get('amount_cents', 0) / 100:.2f}.

We believe this chargeback is invalid based on the following evidence:

{evidence_lines}

We respectfully request that this chargeback be reversed in favor of the merchant.

Sincerely,
Toast Fraud Guardian (Automated Dispute System)
"""


# --- Graph Builder ---

def build_dispute_workflow() -> StateGraph:
    """
    Build the LangGraph workflow for dispute processing.
    Returns a compiled graph ready for execution.
    """
    workflow = StateGraph(WorkflowState)

    # Add nodes
    workflow.add_node("transaction_scorer", transaction_scorer_node)
    workflow.add_node("evidence_collector", evidence_collector_node)
    workflow.add_node("evidence_writer", evidence_writer_node)
    workflow.add_node("decision_engine", decision_engine_node)
    workflow.add_node("dispute_submitter", dispute_submitter_node)

    # Define edges (linear pipeline with conditional routing at decision)
    workflow.set_entry_point("transaction_scorer")
    workflow.add_edge("transaction_scorer", "evidence_collector")
    workflow.add_edge("evidence_collector", "evidence_writer")
    workflow.add_edge("evidence_writer", "decision_engine")
    workflow.add_conditional_edges("decision_engine", route_after_decision)
    workflow.add_edge("dispute_submitter", END)

    return workflow


# Compiled graph singleton
_compiled_graph = None


def get_dispute_graph():
    """Get compiled dispute workflow graph."""
    global _compiled_graph
    if _compiled_graph is None:
        graph = build_dispute_workflow()
        _compiled_graph = graph.compile()
    return _compiled_graph


async def run_dispute_workflow(
    dispute_id: str,
    chargeback: ChargebackEvent,
) -> WorkflowState:
    """
    Execute the full dispute workflow for a chargeback event.
    Returns the final workflow state.
    """
    start = time.monotonic()

    initial_state: WorkflowState = {
        "dispute_id": dispute_id,
        "chargeback_event": chargeback.model_dump(mode="json"),
        "transaction": None,
        "fraud_score_result": None,
        "evidence_items": [],
        "evidence_strength": None,
        "dispute_letter": None,
        "decision": None,
        "decision_rationale": None,
        "errors": [],
        "current_step": "initialized",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
    }

    graph = get_dispute_graph()
    final_state = await graph.ainvoke(initial_state)

    elapsed = time.monotonic() - start
    AGENT_WORKFLOW_LATENCY.observe(elapsed)

    logger.info(
        "agent.workflow.completed",
        dispute_id=dispute_id,
        decision=final_state.get("decision"),
        elapsed_s=round(elapsed, 3),
        errors=final_state.get("errors"),
    )

    return final_state
