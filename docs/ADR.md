# Architecture Decision Records

## ADR-001: Sequence-Based Fraud Scoring with GRU Encoder

**Status**: Accepted
**Date**: 2025-06-01

### Context
Restaurant payment fraud requires detecting anomalous patterns across transaction sequences, not just individual transactions. A customer's behavioral history (spending cadence, typical order sizes, location patterns) provides strong signal for distinguishing legitimate chargebacks from friendly fraud.

### Decision
Use a GRU-based sequence encoder paired with a dense feature network. The GRU processes the last 20 transactions for a given card token, while the dense network processes real-time transaction features. Outputs are fused for a single fraud probability score.

### Rationale
- GRU is more computationally efficient than Transformer for short sequences (N=20) with limited feature dimension (4)
- Unidirectional processing matches the temporal nature of transaction sequences
- ONNX export for GRU is mature and well-tested
- Heuristic fallback ensures zero-downtime if model fails

### Consequences
- Requires Redis-backed sequence cache with 30-day retention
- Cold-start problem for new cards (mitigated by heuristic fallback + "card_is_new" feature)
- Training pipeline needs labeled feedback loop data

---

## ADR-002: LangGraph for Agent Workflow Orchestration

**Status**: Accepted
**Date**: 2025-06-01

### Context
Chargeback dispute processing involves multiple sequential steps (scoring, evidence collection, letter writing, decision, submission) with conditional branching and error handling. We need a framework that supports async execution, state management, and is testable at the node level.

### Decision
Use LangGraph (from LangChain ecosystem) as the workflow engine with typed state passing between nodes.

### Rationale
- First-class async support
- Conditional edges enable auto-submit vs. human-review routing
- Each node is independently testable
- State is explicitly typed (TypedDict), avoiding hidden mutation
- Graph compilation catches structural errors at init time

### Consequences
- LangGraph dependency adds LangChain ecosystem to the stack
- State must be JSON-serializable for Celery task passing
- Graph is compiled once at startup (singleton pattern)

---

## ADR-003: Token-Only Processing (PCI Compliance)

**Status**: Accepted
**Date**: 2025-06-01

### Context
PCI DSS mandates strict controls around cardholder data. Storing PAN, CVV, or other card details would require PCI Level 1 certification for this service.

### Decision
The system never receives, stores, or logs raw card data. All card references use processor-generated tokens (e.g., `tok_abc123` from Stripe). The `TransactionEvent` model validates that `card_token` starts with a processor prefix.

### Rationale
- Eliminates PCI scope for this service entirely
- Payment processors (Stripe, Square, Toast) handle tokenization
- Token-based correlation is sufficient for fraud analysis
- Pydantic validator enforces the pattern at the API boundary

### Consequences
- Cannot perform PAN-level analytics (e.g., BIN analysis) without processor cooperation
- Cross-processor card linking requires processor-level data sharing agreements
- Token rotation by processors could break behavioral sequence continuity

---

## ADR-004: Adapter Pattern for Payment Processors

**Status**: Accepted
**Date**: 2025-06-01

### Context
The system must integrate with multiple payment processors (Stripe, Square, Toast) with significantly different APIs. Toast's API is not publicly documented at the needed level.

### Decision
Define an abstract `PaymentAdapter` interface with concrete implementations per processor. Toast uses an interface-first implementation with sandbox mocks and clearly marked TODO integration points.

### Rationale
- Uniform interface simplifies the agent workflow (processor-agnostic)
- New processors can be added without modifying core logic
- Toast mock enables full-stack testing before API access is available
- Retry/idempotency patterns are per-adapter but contract-enforced

### Consequences
- Adapter registry must be maintained as processors are added
- Toast integration is blocked on partner API access
- Webhook signature verification is processor-specific (cannot be abstracted)

---

## ADR-005: Async-First with Fail-Open Scoring

**Status**: Accepted
**Date**: 2025-06-01

### Context
Pre-auth fraud scoring is latency-critical (sub-200ms). Any blocking call or unhandled error in the scoring path could delay or block payment authorization.

### Decision
The scoring pipeline is fully async. If the ONNX model fails to load or inference errors, the engine falls back to a heuristic rule-based scorer and returns a REVIEW decision. The system never blocks or fails closed on scoring errors.

### Rationale
- Restaurant POS systems have tight timeout budgets
- A false negative (approving fraud) is recoverable via chargeback; a timeout (blocking a sale) is not
- Heuristic fallback provides reasonable risk assessment using interpretable rules
- Model version is tagged on every score for debugging fallback usage

### Consequences
- Heuristic fallback will have lower precision than the ML model
- Monitoring must alert on elevated fallback usage (indicates model health issue)
- "fail-open" means some fraud may pass during model outages
