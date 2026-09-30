from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import models

from app.services.embedding_service import EmbeddingService
from app.services.qdrant_service import QdrantVectorStore


@dataclass(frozen=True)
class SemanticSearchResult:
    payload: dict[str, object]
    score: float


class SemanticSearchService:
    def __init__(self, embedding_service: EmbeddingService, vector_store: QdrantVectorStore) -> None:
        self.embedding_service = embedding_service
        self.vector_store = vector_store

    def search(
        self,
        query: str,
        limit: int = 5,
        query_filter: models.Filter | None = None,
    ) -> list[SemanticSearchResult]:
        query_vector = self.embedding_service.embed_text(query)
        points = self.vector_store.search(query_vector, limit=limit, query_filter=query_filter)
        return [
            SemanticSearchResult(payload=dict(point.payload or {}), score=float(point.score))
            for point in points
        ]