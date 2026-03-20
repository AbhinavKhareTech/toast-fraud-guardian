"""
Application configuration using pydantic-settings.
All config is environment-driven with sensible defaults for development.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    app_env: Environment = Environment.DEVELOPMENT
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_workers: int = 4
    log_level: str = "INFO"
    secret_key: SecretStr = SecretStr("change-me-in-production")

    # Database
    database_url: str = "postgresql+asyncpg://fraud_user:fraud_pass@localhost:5432/fraud_guardian"
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_feature_store_db: int = 1
    redis_sequence_cache_db: int = 2
    redis_max_connections: int = 50

    # Celery
    celery_broker_url: str = "redis://localhost:6379/3"
    celery_result_backend: str = "redis://localhost:6379/4"

    # ML / Inference
    onnx_model_path: str = "./ml/export/fraud_scorer_v1.onnx"
    fraud_score_threshold_auto: float = 0.85
    fraud_score_threshold_review: float = 0.50
    model_version: str = "v1.0.0"

    # LLM
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_tokens: int = 2048
    llm_temperature: float = 0.1

    # Payment integrations
    stripe_api_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    square_access_token: SecretStr = SecretStr("")
    square_webhook_signature_key: SecretStr = SecretStr("")
    toast_client_id: str = ""
    toast_client_secret: SecretStr = SecretStr("")
    toast_environment: Literal["sandbox", "production"] = "sandbox"

    # Observability
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "toast-fraud-guardian"
    prometheus_port: int = 9090

    # Compliance
    pii_retention_days: int = 90
    audit_log_retention_days: int = 2555  # ~7 years
    enable_gdpr_deletion: bool = True

    # Feature flags
    ff_auto_submit_disputes: bool = False
    ff_sequence_model_enabled: bool = True
    ff_llm_evidence_writer: bool = True
    ff_ab_test_scoring: bool = False

    @field_validator("fraud_score_threshold_auto")
    @classmethod
    def validate_auto_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("fraud_score_threshold_auto must be between 0.0 and 1.0")
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
