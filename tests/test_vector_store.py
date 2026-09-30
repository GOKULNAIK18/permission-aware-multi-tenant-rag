from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from qdrant_client import QdrantClient, models

from app.services.embedding_service import EmbeddingService
from app.services.qdrant_service import QdrantVectorStore
from app.services.semantic_search_service import SemanticSearchService


class FakeSentenceTransformer:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.vector_dimension = int(model_name.rsplit("-", 1)[-1]) if model_name.rsplit("-", 1)[-1].isdigit() else 3

    def get_sentence_embedding_dimension(self) -> int:
        return self.vector_dimension

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        vectors = []
        for text in texts:
            normalized = text.lower()
            vectors.append(
                [
                    float("policy" in normalized),
                    float("leave" in normalized),
                    float("salary" in normalized),
                    *([0.0] * (self.vector_dimension - 3)),
                ]
            )
        return vectors


class RecordingQdrantClient:
    def __init__(self) -> None:
        self.client = QdrantClient(":memory:")
        self.index_requests: list[tuple[str, str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def create_payload_index(self, collection_name: str, field_name: str, **kwargs: Any) -> Any:
        self.index_requests.append((collection_name, field_name, kwargs["field_schema"]))
        return self.client.create_payload_index(collection_name, field_name, **kwargs)


@pytest.fixture
def embedding_service() -> EmbeddingService:
    return EmbeddingService(model_name="test-model", model_factory=FakeSentenceTransformer)


def test_embedding_generation_and_configured_dimension(embedding_service: EmbeddingService) -> None:
    assert embedding_service.dimension == 3
    vector = embedding_service.embed_text("leave policy")
    assert vector == [1.0, 1.0, 0.0]


def test_collection_creation_and_payload_indexes_use_model_dimension(
    embedding_service: EmbeddingService,
) -> None:
    client = RecordingQdrantClient()
    store = QdrantVectorStore(client, embedding_service.dimension)

    store.ensure_collection()

    collection = client.get_collection("chunks")
    assert collection.config.params.vectors.size == 3
    assert {field for _, field, _ in client.index_requests} == {
        "tenant_id",
        "allowed_roles",
        "allowed_users",
        "document_id",
    }
    assert dict((field, schema) for _, field, schema in client.index_requests) == {
        "tenant_id": "integer",
        "allowed_roles": "keyword",
        "allowed_users": "integer",
        "document_id": "integer",
    }


def test_collection_dimension_tracks_configured_model() -> None:
    embeddings = EmbeddingService(model_name="configured-model-5", model_factory=FakeSentenceTransformer)
    client = RecordingQdrantClient()
    store = QdrantVectorStore(client, embeddings.dimension)

    store.ensure_collection()

    assert embeddings.dimension == 5
    assert client.get_collection("chunks").config.params.vectors.size == 5


def test_vector_insertion_has_exact_payload_and_requires_tenant(
    embedding_service: EmbeddingService,
) -> None:
    client = RecordingQdrantClient()
    store = QdrantVectorStore(client, embedding_service.dimension)
    store.ensure_collection()
    payload = {
        "chunk_id": "101-chunk-0000",
        "document_id": 101,
        "tenant_id": 7,
        "text": "Company leave policy",
        "allowed_roles": ["HR"],
        "allowed_users": [],
        "classification": "INTERNAL",
    }

    store.upsert_chunks([payload], [[1.0, 1.0, 0.0]])

    point_id = str(uuid5(NAMESPACE_URL, "101-chunk-0000"))
    stored = client.retrieve("chunks", ids=[point_id], with_payload=True)[0]
    assert stored.payload == payload
    assert set(stored.payload or {}) == {
        "chunk_id",
        "document_id",
        "tenant_id",
        "text",
        "allowed_roles",
        "allowed_users",
        "classification",
    }

    missing_tenant = {key: value for key, value in payload.items() if key != "tenant_id"}
    before_count = client.count("chunks").count
    with pytest.raises(ValueError, match="tenant_id"):
        store.upsert_chunks([missing_tenant], [[1.0, 1.0, 0.0]])
    assert client.count("chunks").count == before_count


def test_semantic_search_returns_payload_and_accepts_future_filter(
    embedding_service: EmbeddingService,
) -> None:
    client = RecordingQdrantClient()
    store = QdrantVectorStore(client, embedding_service.dimension)
    store.ensure_collection()
    store.upsert_chunks(
        [
            {
                "chunk_id": "doc-a-chunk-0000",
                "document_id": 1,
                "tenant_id": 4,
                "text": "The leave policy allows annual leave.",
                "allowed_roles": ["HR"],
                "allowed_users": [],
                "classification": "INTERNAL",
            },
            {
                "chunk_id": "doc-b-chunk-0000",
                "document_id": 2,
                "tenant_id": 5,
                "text": "Salary report details.",
                "allowed_roles": ["ADMIN"],
                "allowed_users": [],
                "classification": "CONFIDENTIAL",
            },
        ],
        [[1.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    )
    search = SemanticSearchService(embedding_service, store)

    tenant_filter = models.Filter(
        must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=4))]
    )
    results = search.search("leave policy", limit=1, query_filter=tenant_filter)

    assert len(results) == 1
    assert results[0].payload["chunk_id"] == "doc-a-chunk-0000"
    assert results[0].payload["tenant_id"] == 4
    assert str(results[0].payload["text"]).startswith("The leave policy")
