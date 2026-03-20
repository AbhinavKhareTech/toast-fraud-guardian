"""
LLM abstraction layer.
Supports Anthropic Claude and OpenAI with:
- Structured output (no chain-of-thought leakage)
- Prompt templates for dispute letters and evidence summarization
- Retry logic and error handling
- Token usage tracking
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


class LLMResponse:
    """Structured LLM response with metadata."""

    __slots__ = ("content", "model", "usage_tokens", "latency_ms", "raw")

    def __init__(
        self,
        content: str,
        model: str,
        usage_tokens: dict[str, int],
        latency_ms: float,
        raw: Any = None,
    ):
        self.content = content
        self.model = model
        self.usage_tokens = usage_tokens
        self.latency_ms = latency_ms
        self.raw = raw


class LLMClient(ABC):
    """Abstract LLM client interface."""

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class AnthropicClient(LLMClient):
    """Anthropic Claude client with structured output."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self._api_key = api_key or settings.anthropic_api_key.get_secret_value()
        self._model = model or settings.llm_model
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> LLMResponse:
        client = await self._get_client()
        start = time.monotonic()

        response = await client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        elapsed_ms = (time.monotonic() - start) * 1000
        content = response.content[0].text if response.content else ""

        return LLMResponse(
            content=content,
            model=self._model,
            usage_tokens={
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
            latency_ms=round(elapsed_ms, 2),
            raw=response,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None


class OpenAIClient(LLMClient):
    """OpenAI client with structured output."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self._api_key = api_key or settings.openai_api_key.get_secret_value()
        self._model = model or "gpt-4o"
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> LLMResponse:
        client = await self._get_client()
        start = time.monotonic()

        response = await client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        elapsed_ms = (time.monotonic() - start) * 1000
        content = response.choices[0].message.content or ""

        return LLMResponse(
            content=content,
            model=self._model,
            usage_tokens={
                "input": response.usage.prompt_tokens if response.usage else 0,
                "output": response.usage.completion_tokens if response.usage else 0,
            },
            latency_ms=round(elapsed_ms, 2),
            raw=response,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None


def get_llm_client() -> LLMClient:
    """Factory: returns the configured LLM client."""
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        return AnthropicClient()
    elif settings.llm_provider == "openai":
        return OpenAIClient()
    else:
        raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


# --- Prompt Templates ---
# These are structured to produce deterministic, auditable output.
# NO chain-of-thought leakage: reasoning is internal, output is structured.

DISPUTE_LETTER_SYSTEM_PROMPT = """\
You are a chargeback dispute specialist for a restaurant payment processor.
Your role is to write compelling, factual dispute response letters.

RULES:
- Output ONLY the dispute letter text. No preamble, no explanation.
- Be professional, concise, and factual.
- Reference specific evidence provided.
- Follow the card network's representment guidelines.
- Never fabricate evidence or make claims not supported by the provided data.
- Structure: greeting, transaction summary, evidence points, conclusion.
- Keep under 500 words.
"""

DISPUTE_LETTER_USER_TEMPLATE = """\
Write a dispute response letter for this chargeback:

TRANSACTION:
- Transaction ID: {transaction_id}
- Merchant: {merchant_name} (ID: {merchant_id})
- Amount: ${amount:.2f}
- Date: {transaction_date}
- Type: {transaction_type}
- Reason Code: {reason_code} - {reason_description}

EVIDENCE:
{evidence_summary}

FRAUD SCORE: {fraud_score:.2f} (model assessment of fraud likelihood)
BEHAVIORAL SIGNALS: {behavioral_signals}

Write the dispute response letter now.
"""

EVIDENCE_SUMMARY_SYSTEM_PROMPT = """\
You are an evidence analyst for payment disputes.
Summarize the collected evidence into a structured assessment.

Output ONLY a JSON object with these fields:
- "strength": "high" | "medium" | "low" | "insufficient"
- "summary": string (2-3 sentence summary)
- "key_points": list of strings (bullet points for the dispute letter)
- "weaknesses": list of strings (gaps in evidence)
- "recommendation": "auto_submit" | "human_review" | "decline_dispute"
"""

EVIDENCE_SUMMARY_USER_TEMPLATE = """\
Assess this dispute evidence:

CHARGEBACK:
- Reason: {reason_code} - {reason_description}
- Amount: ${amount:.2f}

EVIDENCE ITEMS:
{evidence_items_text}

FRAUD ANALYSIS:
- Fraud Score: {fraud_score:.2f}
- Anomaly Flags: {anomaly_flags}

Respond with JSON only.
"""
