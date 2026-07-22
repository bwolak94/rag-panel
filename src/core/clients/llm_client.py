"""Async OpenAI-compatible LLM client for chat completions and embeddings.

Used by ingest graph nodes (validate, embed) and query graph (generate).
Never log prompt content or response content — only token counts and latency.
"""

from __future__ import annotations

from typing import Any

import structlog
from openai import AsyncOpenAI

logger = structlog.get_logger(__name__)


class LLMClient:
    """Thin async wrapper around the OpenAI-compatible API.

    One instance can be reused across multiple graph invocations.
    The base_url can be overridden per-call to support multiple endpoints
    (e.g. different Ollama instances for LLM vs embedding models).
    """

    def __init__(self, base_url: str = "http://ollama:11434/v1", api_key: str = "none") -> None:
        self._default_base_url = base_url
        self._api_key = api_key

    def _client(self, base_url: str | None = None) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url=base_url or self._default_base_url,
            api_key=self._api_key,
        )

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        base_url: str | None = None,
        response_format: dict[str, str] | None = None,
        temperature: float = 0.0,
    ) -> Any:
        """Call the chat completions endpoint.

        Args:
            model: Model identifier (e.g. "llama3.2").
            messages: List of {"role": ..., "content": ...} dicts.
            base_url: Override the default endpoint URL.
            response_format: e.g. {"type": "json_object"}.
            temperature: Sampling temperature.

        Returns:
            OpenAI ChatCompletion response object.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            kwargs["response_format"] = response_format

        client = self._client(base_url)
        return await client.chat.completions.create(**kwargs)

    async def embeddings(
        self,
        model: str,
        input: list[str],
        *,
        base_url: str | None = None,
    ) -> Any:
        """Call the embeddings endpoint.

        Args:
            model: Embedding model identifier.
            input: List of texts to embed.
            base_url: Override the default endpoint URL.

        Returns:
            OpenAI CreateEmbeddingResponse object (response.data[i].embedding).
        """
        client = self._client(base_url)
        return await client.embeddings.create(model=model, input=input)
