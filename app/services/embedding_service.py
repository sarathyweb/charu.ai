"""Azure OpenAI embedding client and vector helpers."""

import math
from collections.abc import Sequence
from typing import Protocol

from openai import AsyncAzureOpenAI

from app.config import Settings


class TextEmbeddingService(Protocol):
    """Minimal embedding interface used by task deduplication."""

    model: str

    async def embed_text(self, text: str) -> list[float]:
        """Return an embedding vector for *text*."""


class AzureOpenAIEmbeddingService:
    """Generate embeddings through an Azure OpenAI deployment."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        api_version: str,
        model: str,
        dimensions: int | None = None,
        client: AsyncAzureOpenAI | None = None,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.api_version = api_version
        self.model = model
        self.dimensions = dimensions
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> "AzureOpenAIEmbeddingService | None":
        """Build a service from env-backed settings when fully configured."""
        if not (
            settings.AZURE_OPENAI_API_KEY
            and settings.AZURE_OPENAI_ENDPOINT
            and settings.AZURE_OPENAI_API_VERSION
            and settings.AZURE_OPENAI_EMBEDDING_MODEL
        ):
            return None

        return cls(
            api_key=settings.AZURE_OPENAI_API_KEY,
            endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_version=settings.AZURE_OPENAI_API_VERSION,
            model=settings.AZURE_OPENAI_EMBEDDING_MODEL,
            dimensions=settings.AZURE_OPENAI_EMBEDDING_DIMENSIONS,
        )

    async def embed_text(self, text: str) -> list[float]:
        """Generate one embedding vector for trimmed task text."""
        input_text = text.strip()
        if not input_text:
            raise ValueError("Cannot embed empty text.")

        kwargs: dict[str, object] = {
            "model": self.model,
            "input": input_text,
        }
        if self.dimensions is not None:
            kwargs["dimensions"] = self.dimensions

        response = await self._get_client().embeddings.create(**kwargs)
        if not response.data:
            raise ValueError("Azure OpenAI returned no embedding data.")

        return [float(value) for value in response.data[0].embedding]

    def _get_client(self) -> AsyncAzureOpenAI:
        if self._client is None:
            self._client = AsyncAzureOpenAI(
                api_key=self.api_key,
                azure_endpoint=self.endpoint,
                api_version=self.api_version,
            )
        return self._client


def cosine_similarity(
    left: Sequence[float] | None,
    right: Sequence[float] | None,
) -> float:
    """Return cosine similarity for two equal-length vectors."""
    if not left or not right or len(left) != len(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0

    return dot / (left_norm * right_norm)
