# Operations Runbook

## Deployment

### First-Time Setup
```bash
# 1. Clone and configure
git clone <repo>
cd toast-fraud-guardian
cp .env.example .env
# Edit .env with production credentials

# 2. Start infrastructure
docker-compose up -d postgres redis

# 3. Run migrations
docker-compose run --rm app alembic upgrade head

# 4. Train and export model (or deploy pre-trained)
docker-compose run --rm app python -m ml.training.train --epochs 50
docker-compose run --rm app python -m ml.export.to_onnx \
    --checkpoint ml/training/checkpoints/best.pt \
    --output ml/export/fraud_scorer_v1.onnx

# 5. Start all services
docker-compose up -d
```

### Rolling Update
```bash
# Build new image
docker build -t toast-fraud-guardian:v1.x.x .

# Kubernetes rolling update
kubectl set image deployment/fraud-guardian-api \
    api=toast-fraud-guardian:v1.x.x \
    -n fraud-guardian

# Verify
kubectl rollout status deployment/fraud-guardian-api -n fraud-guardian
```

### Model Hot-Swap
```bash
# 1. Register new model version
curl -X POST http://localhost:8000/api/v1/admin/models/register \
    -H "Content-Type: application/json" \
    -d '{"version": "v1.1.0", "model_path": "/app/ml/export/v1.1.0.onnx", "metrics": {"auc": 0.94}}'

# 2. Activate (hot-swaps without restart)
curl -X POST http://localhost:8000/api/v1/admin/models/v1.1.0/activate

# 3. Verify
curl http://localhost:8000/api/v1/scoring/health
```

## Monitoring

### Key Metrics to Watch

| Metric | Threshold | Action |
|--------|-----------|--------|
| `fraud_score_latency_seconds` p99 | > 200ms | Scale API pods, check Redis latency |
| `model_inference_errors_total` rate | > 0.01/s | Check model health, verify ONNX file |
| `disputes_automation_rate` | < 85% | Review decision thresholds, evidence quality |
| `active_review_queue_size` | > 100 | Scale reviewers, check for stuck disputes |
| `webhook_received_total` spike | 5x normal | Possible fraud wave, check processor status |

### Health Check Endpoints
- `/health` - Basic liveness
- `/ready` - DB + Redis connectivity
- `/metrics` - Prometheus metrics
- `/api/v1/scoring/health` - Model readiness

## Troubleshooting

### Scoring Engine in Fallback Mode
**Symptom**: `model_version` in responses contains `-heuristic`
**Cause**: ONNX model failed to load
**Fix**:
1. Check logs: `docker-compose logs app | grep scorer`
2. Verify model file exists: `ls -la ml/export/fraud_scorer_v1.onnx`
3. Re-export: `python -m ml.export.to_onnx --checkpoint ml/training/checkpoints/best.pt`
4. Restart: `docker-compose restart app`

### Celery Workers Not Processing
**Symptom**: Disputes stuck in "received" status
**Fix**:
1. Check worker logs: `docker-compose logs worker-disputes`
2. Verify Redis broker: `redis-cli -n 3 LLEN disputes`
3. Restart workers: `docker-compose restart worker-disputes`

### High Scoring Latency
**Symptom**: `fraud_score_latency_seconds` p99 > 200ms
**Fix**:
1. Check Redis latency: `redis-cli --latency`
2. Check feature store key count: `redis-cli -n 1 DBSIZE`
3. Scale API pods: `kubectl scale deployment fraud-guardian-api --replicas=6`

### GDPR Deletion Request
```bash
# Via API
curl -X POST http://localhost:8000/api/v1/admin/gdpr/delete \
    -H "Content-Type: application/json" \
    -d '{"entity_type": "merchant", "entity_id": "merchant_042", "requested_by": "legal@company.com"}'

# Via Celery directly
celery -A workers.tasks call workers.retention.process_deletion_request \
    --args='["merchant", "merchant_042", "legal@company.com"]'
```

## Celery Beat Schedule

| Task | Schedule | Queue |
|------|----------|-------|
| PII retention enforcement | Daily 3:00 AM UTC | maintenance |
| Audit log retention | Weekly Sunday 4:00 AM | maintenance |
| Model health check | Hourly | maintenance |
| Deadline alerts | Every 6 hours | disputes |
| Metrics snapshot | Every 30 minutes | maintenance |

Start beat scheduler:
```bash
celery -A workers.beat_schedule beat --loglevel=info
```
