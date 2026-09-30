from __future__ import annotations

import logging
import re
from typing import Protocol
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings
from app.services.prompt_service import ChatPrompt

logger = logging.getLogger(__name__)

_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(authorization\s*[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|Bearer\s+[^\s,}\]]+|[^\s,}\]]+)"
)
_SECRET_FIELD_VALUE = re.compile(
    r"(?i)((?:api[_ -]?key|key|access[_ -]?token|refresh[_ -]?token|token|secret|password)"
    r"\s*[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s,}\]]+")


class LLMClient(Protocol):
    def complete(self, prompt: ChatPrompt) -> str: ...


class OpenAICompatibleLLMClient:
    """Client for providers exposing the OpenAI chat-completions API shape."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.Client(timeout=timeout_seconds)

    def complete(self, prompt: ChatPrompt) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = self._http_client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": prompt.system_message},
                        {"role": "user", "content": prompt.user_message},
                    ],
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "LLM provider HTTP failure status_code=%d endpoint=%s model=%s response_body=%s",
                exc.response.status_code,
                self._safe_endpoint(),
                self._safe_model(),
                self._safe_error_body(exc.response.text),
            )
            raise RuntimeError("LLM provider request failed.") from None
        except httpx.HTTPError:
            logger.error(
                "LLM provider transport failure endpoint=%s model=%s",
                self._safe_endpoint(),
                self._safe_model(),
            )
            raise RuntimeError("LLM provider request failed.") from None
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM provider returned an invalid chat-completions response.") from exc
        if not isinstance(content, str):
            raise RuntimeError("LLM provider returned non-text message content.")
        return content

    def _safe_error_body(self, body: str) -> str:
        body = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", body)
        body = _SECRET_FIELD_VALUE.sub(r"\1[REDACTED]", body)
        body = _BEARER_VALUE.sub("Bearer [REDACTED]", body)
        body = self._redact_known_secret(body)
        compact = " ".join(body.split())
        return compact[:1500] if compact else "<empty>"

    def _redact_known_secret(self, value: str) -> str:
        if not self.api_key:
            return value
        variants = {
            self.api_key,
            quote(self.api_key, safe=""),
            quote_plus(self.api_key),
        }
        for secret in sorted((item for item in variants if item), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    def _safe_endpoint(self) -> str:
        try:
            parsed = urlsplit(self.base_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return "<configured-endpoint>"
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            path = f"{parsed.path.rstrip('/')}/chat/completions"
            endpoint = urlunsplit((parsed.scheme.lower(), host, path, "", ""))
            return self._redact_known_secret(endpoint)
        except ValueError:
            return "<configured-endpoint>"

    def _safe_model(self) -> str:
        return self._redact_known_secret(" ".join(self.model.split()))[:120]

    def close(self) -> None:
        if self._owns_client:
            self._http_client.close()

    def __enter__(self) -> OpenAICompatibleLLMClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class MockLLMClient:
    """Deterministic LLM replacement for local security testing."""

    def complete(self, prompt: ChatPrompt) -> str:
        return (
            "Mock LLM response: The authorized document context was "
            "successfully retrieved and passed to the LLM."
        )


def create_llm_client() -> LLMClient:
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()

    if provider == "mock":
        return MockLLMClient()

    if provider != "openai-compatible":
        raise ValueError(f"Unsupported LLM provider: {settings.llm_provider!r}.")
    return OpenAICompatibleLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )
