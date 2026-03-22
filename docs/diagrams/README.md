# Architecture Diagrams

Mermaid diagrams for the Toast Fraud Guardian system. Render natively on GitHub or any Mermaid-compatible viewer.

| # | Diagram | Description |
|---|---------|-------------|
| 01 | [System Context](01-system-context.md) | High-level system overview with all external integrations |
| 02 | [Risk Evaluation Sequence](02-risk-evaluation-sequence.md) | Real-time scoring pipeline: API → features → ONNX → decision |
| 03 | [Agent Workflow State](03-agent-workflow-state.md) | LangGraph 5-node dispute workflow with all branching paths |
| 04 | [Chargeback Flow Sequence](04-chargeback-flow-sequence.md) | Full webhook → agent → submission sequence with human review |
| 05 | [Model Architecture](05-model-architecture.md) | GRU encoder + dense fusion network with ONNX export |
| 06 | [Feature Engineering](06-feature-engineering.md) | Real-time feature extraction pipeline from Redis |
| 07 | [Adapter Pattern](07-adapter-pattern.md) | Payment processor abstraction: Stripe, Square, Toast |
| 08 | [Data Model](08-data-model.md) | PostgreSQL ER diagram: disputes, scores, audit, models |
| 09 | [Deployment Topology](09-deployment-topology.md) | Kubernetes architecture with HPA and worker topology |
| 10 | [PCI Security Boundaries](10-pci-security-boundaries.md) | PCI compliance scope and PII protection layers |
| 11 | [Evidence Decision Flow](11-evidence-decision-flow.md) | Evidence strength assessment and automation decision logic |
| 12 | [Training Feedback Loop](12-training-feedback-loop.md) | Model retraining pipeline with feedback from dispute outcomes |
| 13 | [Celery Task Routing](13-celery-task-routing.md) | Worker topology, queue routing, and task registry |
| 14 | [Data Retention GDPR](14-data-retention-gdpr.md) | PII retention enforcement and GDPR deletion flow |
| 15 | [CI/CD Pipeline](15-cicd-pipeline.md) | GitHub Actions: lint → test → security → build → deploy |
| 16 | [Dispute Lifecycle](16-dispute-lifecycle.md) | Full dispute state machine from receipt to resolution |
