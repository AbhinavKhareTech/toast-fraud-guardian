# toast-fraud-guardian

Real-time fraud detection and autonomous chargeback dispute system for US restaurant merchants at scale.

## Targets

| Metric | Target |
|---|---|
| Dispute automation rate | ≥ 85% |
| Dispute win rate | ≥ 70% |
| Fraud scoring latency | < 200ms (p99) |
| Throughput | 50K TPS capacity |
| Compliance | PCI DSS, CCPA, GDPR |

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Webhooks /  │────▶│  FastAPI Gateway  │────▶│  Fraud Scoring  │
│  REST API    │     │  (async-first)    │     │  (ONNX Runtime) │
└──────────────┘     └────────┬─────────┘     └────────┬────────┘
                              │                        │
                     ┌────────▼─────────┐     ┌────────▼────────┐
                     │  LangGraph Agent │     │  Redis Feature  │
                     │  Workflow Engine  │     │  Store + Cache  │
                     └────────┬─────────┘     └─────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
     ┌────────────┐  ┌──────────────┐  ┌────────────┐
     │  Evidence   │  │  Decision    │  │  Dispute   │
     │  Collector  │  │  Engine      │  │  Submitter │
     └────────────┘  └──────────────┘  └────────────┘
              │               │               │
              └───────────────┼───────────────┘
                              ▼
                     ┌────────────────┐
                     │  PostgreSQL    │
                     │  (Audit Trail) │
                     └────────────────┘
```

### Core Stack

- **FastAPI** — async-first API gateway with rate limiting
- **LangGraph** — multi-agent workflow for dispute automation
- **PostgreSQL** — audit trail, dispute records, feedback loop
- **Redis** — feature store, behavioral sequence cache, rate limiting
- **Celery** — async workers for evidence generation, model retraining
- **ONNX Runtime** — sub-200ms fraud scoring inference
- **Prometheus + OpenTelemetry** — observability

### AI Layer

1. **Fraud Scoring Engine** — sequence-based behavioral model (GRU encoder + feature aggregation)
2. **LangGraph Agent Workflow** — transaction_scorer → evidence_collector → evidence_writer → decision_engine → dispute_submitter
3. **LLM Abstraction** — Claude/OpenAI compatible with structured output (no chain-of-thought leakage)

## Quick Start

```bash
cp .env.example .env
# Edit .env with your credentials
docker-compose up -d
# Run migrations
docker-compose exec app alembic upgrade head
# Verify
curl http://localhost:8000/health
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Compliance

- **PCI DSS**: No PAN/card storage. Token-based processing only.
- **CCPA/GDPR**: PII minimization layer, configurable retention, right-to-delete support.
- **Audit**: Every decision, score, and submission is logged with full lineage.

## License

Proprietary — All rights reserved.
