"""
Observability: OpenTelemetry tracing + Prometheus metrics.
Provides standardized instrumentation for fraud scoring pipeline.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- Prometheus Metrics ---

FRAUD_SCORE_LATENCY = Histogram(
    "fraud_score_latency_seconds",
    "Latency of fraud scoring inference",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0],
)

FRAUD_SCORE_DISTRIBUTION = Histogram(
    "fraud_score_value",
    "Distribution of fraud scores",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0],
)

TRANSACTIONS_SCORED = Counter(
    "transactions_scored_total",
    "Total transactions scored",
    ["decision"],  # auto_approve, review, auto_decline
)

DISPUTES_CREATED = Counter(
    "disputes_created_total",
    "Total disputes created",
    ["source"],  # webhook, manual
)

DISPUTES_SUBMITTED = Counter(
    "disputes_submitted_total",
    "Total disputes auto-submitted",
    ["outcome"],  # won, lost, pending
)

DISPUTES_AUTOMATION_RATE = Gauge(
    "disputes_automation_rate",
    "Percentage of disputes handled without human intervention",
)

EVIDENCE_GENERATION_LATENCY = Histogram(
    "evidence_generation_latency_seconds",
    "Latency of evidence generation pipeline",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

AGENT_WORKFLOW_LATENCY = Histogram(
    "agent_workflow_latency_seconds",
    "End-to-end latency of LangGraph agent workflow",
    buckets=[1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

MODEL_INFERENCE_ERRORS = Counter(
    "model_inference_errors_total",
    "Total model inference errors",
    ["error_type"],
)

ACTIVE_REVIEW_QUEUE = Gauge(
    "active_review_queue_size",
    "Number of disputes in human review queue",
)

WEBHOOK_RECEIVED = Counter(
    "webhook_received_total",
    "Total webhooks received",
    ["provider", "event_type"],
)


def setup_otel_tracing() -> None:
    """Initialize OpenTelemetry tracing. Safe to call in non-production without collector."""
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from app.core.config import get_settings

        settings = get_settings()
        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)

        if settings.otel_exporter_otlp_endpoint:
            exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
    except Exception:
        # Non-fatal: tracing is optional in development
        pass


def get_tracer(name: str):  # type: ignore[no-untyped-def]
    from opentelemetry import trace

    return trace.get_tracer(name)
