# API Reference

Base URL: `http://localhost:8000`

## System Endpoints

### Health Check
```
GET /health
```
Returns application health status, version, and uptime.

### Readiness Check
```
GET /ready
```
Verifies database and Redis connectivity. Use for Kubernetes readiness probes.

### Prometheus Metrics
```
GET /metrics
```
Exposes Prometheus-format metrics including fraud_score_latency, disputes_created_total, disputes_automation_rate, and more.

## Scoring API

### Score Transaction
```
POST /api/v1/scoring/score
Content-Type: application/json
```

**Request Body:**
```json
{
  "transaction": {
    "transaction_id": "txn_001",
    "merchant_id": "merchant_rest_042",
    "card_token": "tok_stripe_abc123",
    "amount_cents": 4599,
    "currency": "USD",
    "transaction_type": "card_present",
    "timestamp": "2025-06-15T19:30:00Z",
    "payment_processor": "stripe",
    "device_signals": {
      "ip_address_hash": "a1b2c3d4",
      "is_known_proxy": false,
      "is_tor_exit": false,
      "geo_country": "US",
      "geo_city": "Boston"
    },
    "order_metadata": {
      "order_id": "ord_001",
      "item_count": 3,
      "has_alcohol": true,
      "has_tip": true,
      "tip_percentage": 20.0,
      "order_channel": "in_store"
    }
  },
  "include_feature_contributions": true
}
```

**Response (200):**
```json
{
  "score": {
    "transaction_id": "txn_001",
    "merchant_id": "merchant_rest_042",
    "fraud_score": 0.12,
    "decision": "approve",
    "model_version": "v1.0.0",
    "feature_contributions": {"amount_log": 0.85, "hour_sin": 0.3},
    "sequence_risk_score": 0.05,
    "behavioral_anomaly_flags": [],
    "scored_at": "2025-06-15T19:30:01Z",
    "latency_ms": 45.2
  },
  "request_id": "req_abc123"
}
```

**Validation Rules:**
- `card_token` must start with `tok_` or `card_` (raw PANs are rejected with 422)
- `amount_cents` must be > 0
- `transaction_type` must be one of: card_present, card_not_present, contactless, mobile_wallet, online

### Scoring Health
```
GET /api/v1/scoring/health
```
Returns model readiness status and active version.

## Disputes API

### Receive Webhook
```
POST /api/v1/disputes/webhooks/{processor}
```
Processor: `stripe`, `square`, `toast`

Ingests chargeback webhook, verifies signature, creates dispute, and queues agent workflow.

### Get Dispute
```
GET /api/v1/disputes/{dispute_id}
```

### List Disputes
```
GET /api/v1/disputes/?status=pending_review&merchant_id=m1&limit=50
```

### Submit Review Decision
```
POST /api/v1/disputes/{dispute_id}/review
Content-Type: application/json

{
  "decision": "approve",
  "reviewer_id": "reviewer_001",
  "notes": "Evidence is strong, approve submission."
}
```
Valid decisions: `approve` (submits dispute), `reject` (abandons dispute).

### Dispute Metrics
```
GET /api/v1/disputes/metrics/summary
```
Returns automation rate, win rate, and volume counts.

## Admin API

### List Model Versions
```
GET /api/v1/admin/models
```

### Register Model
```
POST /api/v1/admin/models/register
{"version": "v1.1.0", "model_path": "/app/ml/export/v1.1.0.onnx", "metrics": {"auc": 0.94}}
```

### Activate Model (Hot-Swap)
```
POST /api/v1/admin/models/v1.1.0/activate
```

### Feature Flags
```
GET /api/v1/admin/feature-flags
```

### GDPR Deletion Request
```
POST /api/v1/admin/gdpr/delete
{"entity_type": "merchant", "entity_id": "merchant_042", "requested_by": "compliance_team"}
```

### System Config
```
GET /api/v1/admin/system/config
```
Returns non-secret configuration for debugging.

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad request (unknown processor, invalid decision) |
| 401 | Invalid webhook signature |
| 404 | Resource not found |
| 409 | Conflict (dispute not in expected state) |
| 422 | Validation error (raw PAN, zero amount, etc.) |
| 500 | Internal server error |

## Headers

All responses include:
- `X-Request-ID` - Unique request identifier for tracing
- `X-Response-Time-Ms` - Server-side processing time in milliseconds
