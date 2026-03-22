```mermaid
---
title: Payment Adapter Pattern
---
classDiagram
    class PaymentAdapter {
        <<abstract>>
        +processor_name: str*
        +verify_webhook(payload, headers)* bool
        +parse_chargeback_webhook(payload)* PaymentTransaction
        +fetch_transaction(transaction_id)* PaymentTransaction
        +submit_dispute_evidence(submission)* DisputeSubmissionResult
        +get_dispute_status(dispute_id)* dict
    }

    class StripeAdapter {
        -_api_key: str
        -_webhook_secret: str
        -_client: AsyncClient
        +processor_name = "stripe"
        +verify_webhook() HMAC-SHA256 v1 scheme
        +fetch_transaction() GET /v1/charges/{id}
        +submit_dispute_evidence() POST /v1/disputes/{id}
        ~retry: 3 attempts, exponential backoff
        ~idempotency: Idempotency-Key header
    }

    class SquareAdapter {
        -_access_token: str
        -_webhook_key: str
        -_base_url: str
        +processor_name = "square"
        +verify_webhook() HMAC-SHA256 base64
        +fetch_transaction() GET /v2/payments/{id}
        +submit_dispute_evidence() POST /v2/disputes/{id}/evidence-text
        ~sandbox: squareupsandbox.com
        ~production: connect.squareup.com
    }

    class ToastAdapter {
        -_client_id: str
        -_client_secret: str
        -_environment: str
        +processor_name = "toast"
        +verify_webhook() sandbox bypass
        +fetch_transaction() MOCK in sandbox
        +submit_dispute_evidence() MOCK in sandbox
        ~NOTE: Interface-first implementation
        ~TODO: Real API when partner access available
    }

    class PaymentTransaction {
        +transaction_id: str
        +merchant_id: str
        +card_token: str
        +amount_cents: int
        +currency: str
        +transaction_type: str
        +timestamp: datetime
        +processor: str
        +raw_metadata: dict
    }

    class DisputeSubmission {
        +dispute_id: str
        +chargeback_id: str
        +transaction_id: str
        +evidence_text: str
        +evidence_attachments: list
        +metadata: dict
    }

    class DisputeSubmissionResult {
        +success: bool
        +processor_dispute_id: str
        +status: str
        +message: str
        +raw_response: dict
    }

    class AdapterRegistry {
        -_registry: dict
        +get_adapter(processor) PaymentAdapter
        +list_adapters() list~str~
    }

    PaymentAdapter <|-- StripeAdapter
    PaymentAdapter <|-- SquareAdapter
    PaymentAdapter <|-- ToastAdapter
    PaymentAdapter ..> PaymentTransaction : returns
    PaymentAdapter ..> DisputeSubmission : accepts
    PaymentAdapter ..> DisputeSubmissionResult : returns
    AdapterRegistry --> PaymentAdapter : creates/caches
```
