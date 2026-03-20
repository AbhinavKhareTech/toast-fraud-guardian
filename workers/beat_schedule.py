"""
Celery beat schedule configuration.
Defines periodic tasks for data retention, model health checks, and metrics.
"""

from __future__ import annotations

from celery.schedules import crontab

from workers.tasks import celery_app

celery_app.conf.beat_schedule = {
    # --- Data Retention ---
    "enforce-pii-retention-daily": {
        "task": "workers.retention.enforce_pii_retention",
        "schedule": crontab(hour=3, minute=0),  # 3:00 AM UTC daily
        "options": {"queue": "maintenance"},
    },
    "enforce-audit-log-retention-weekly": {
        "task": "workers.retention.enforce_audit_log_retention",
        "schedule": crontab(hour=4, minute=0, day_of_week=0),  # Sunday 4:00 AM UTC
        "options": {"queue": "maintenance"},
    },

    # --- Model Health ---
    "model-health-check-hourly": {
        "task": "workers.monitoring.check_model_health",
        "schedule": crontab(minute=0),  # Every hour on the hour
        "options": {"queue": "maintenance"},
    },

    # --- Dispute Deadline Alerting ---
    "check-approaching-deadlines": {
        "task": "workers.monitoring.check_dispute_deadlines",
        "schedule": crontab(hour="*/6", minute=15),  # Every 6 hours
        "options": {"queue": "disputes"},
    },

    # --- Metrics Snapshot ---
    "dispute-metrics-snapshot": {
        "task": "workers.monitoring.snapshot_dispute_metrics",
        "schedule": crontab(minute="*/30"),  # Every 30 minutes
        "options": {"queue": "maintenance"},
    },
}

# Register the retention tasks module
celery_app.conf.include = [
    "workers.tasks",
    "workers.retention",
    "workers.monitoring",
]
