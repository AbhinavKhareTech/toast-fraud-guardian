```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 
  'background': '#fdfdfe',
  'primaryColor': '#e9ecef',
  'primaryTextColor': '#212529',
  'lineColor': '#6c757d'
}}}%%

graph TB
    subgraph External["External Systems"]
        POS["Restaurant POS<br/>160K+ merchants"]
        STRIPE["Stripe API"]
        SQUARE["Square API"]
        TOAST["Toast API<br/>(sandbox)"]
        VISA["Visa / Mastercard<br/>Card Networks"]
        LLM_EXT["Claude API<br/>Evidence Generation"]
    end

    subgraph TFG["Toast Fraud Guardian"]
        direction TB
        API["FastAPI Gateway<br/>async-first, rate-limited"]
        AGENTS["LangGraph Agent<br/>Workflow Engine"]
        SCORER["Fraud Scoring Engine<br/>ONNX Runtime < 200ms"]
        WORKERS["Celery Workers<br/>disputes, submissions, retention"]
        DASHBOARD["Streamlit Dashboard<br/>Human Review Queue"]
    end

    subgraph Data["Data Layer"]
        PG[("PostgreSQL<br/>Disputes + Audit + Feedback")]
        REDIS[("Redis Cluster<br/>Features + Sequences + Cache")]
    end

    POS -->|"transactions"| STRIPE & SQUARE & TOAST
    STRIPE & SQUARE & TOAST -->|"chargeback webhooks"| API
    API -->|"score request"| SCORER
    API -->|"dispute created"| WORKERS
    WORKERS -->|"execute workflow"| AGENTS
    AGENTS -->|"generate evidence"| LLM_EXT
    AGENTS -->|"submit dispute"| STRIPE & SQUARE & TOAST
    SCORER -->|"feature lookup < 50ms"| REDIS
    AGENTS & WORKERS -->|"persist + audit"| PG
    DASHBOARD -->|"review decisions"| API
    STRIPE & SQUARE & TOAST -.->|"resolution webhooks"| API
    API -.->|"dispute result"| VISA

    %% ──────────────────────────────────────────────
    %% Light, readable styling for both themes
    %% ──────────────────────────────────────────────
    style External fill:#f1f3f5,stroke:#adb5bd,color:#212529
    style TFG      fill:#e3f2fd,stroke:#1976d2,color:#0d47a1,font-weight:bold
    style Data     fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
