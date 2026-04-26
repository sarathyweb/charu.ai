"""Tests for Azure OpenAI embedding helpers."""

from types import SimpleNamespace

import pytest

from app.services.embedding_service import (
    AzureOpenAIEmbeddingService,
    cosine_similarity,
)


class FakeEmbeddingsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    embedding=[0.25, 0.5, 0.75],
                )
            ]
        )


class FakeAzureClient:
    def __init__(self) -> None:
        self.embeddings = FakeEmbeddingsClient()


@pytest.mark.asyncio
async def test_azure_embedding_service_calls_configured_deployment():
    client = FakeAzureClient()
    service = AzureOpenAIEmbeddingService(
        api_key="test-key",
        endpoint="https://example.openai.azure.com/",
        api_version="2025-03-01-preview",
        model="text-embedding-3-large",
        dimensions=256,
        client=client,  # type: ignore[arg-type]
    )

    embedding = await service.embed_text("  Call Sam about the invoice  ")

    assert embedding == [0.25, 0.5, 0.75]
    assert client.embeddings.calls == [
        {
            "model": "text-embedding-3-large",
            "input": "Call Sam about the invoice",
            "dimensions": 256,
        }
    ]


def test_cosine_similarity_handles_expected_cases():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
