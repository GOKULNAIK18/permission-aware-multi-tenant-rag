from __future__ import annotations

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from app.core.config import get_settings

PAYLOAD_FIELDS = frozenset(
    {
        "chunk_id",
        "document_id",
        "tenant_id",
        "text",
        "allowed_roles",
        "allowed_users",
        "classification",
    }
)
PAYLOAD_INDEXES = {
    "tenant_id": models.PayloadSchemaType.INTEGER,
    "allowed_roles": models.PayloadSchemaType.KEYWORD,
    "allowed_users": models.PayloadSchemaType.INTEGER,
    "document_id": models.PayloadSchemaType.INTEGER,
}


def create_qdrant_client() -> QdrantClient:
    settings = get_settings()
    if settings.qdrant_url == ":memory:":
        return QdrantClient(":memory:")
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


class QdrantVectorStore:
    """Qdrant collection operations, independent of embedding implementation."""

    def __init__(
        self,
        client: QdrantClient,
        vector_size: int,
        collection_name: str | None = None,
    ) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size must be greater than zero.")
        settings = get_settings()
        self.client = client
        self.vector_size = vector_size
        self.collection_name = collection_name or settings.qdrant_collection

    def ensure_collection(self) -> None:
        existing = {item.name for item in self.client.get_collections().collections}
        if self.collection_name not in existing:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(size=self.vector_size, distance=models.Distance.COSINE),
            )
        else:
            collection = self.client.get_collection(self.collection_name)
            vector_config = collection.config.params.vectors
            if not isinstance(vector_config, models.VectorParams) or vector_config.size != self.vector_size:
                raise ValueError(
                    f"Qdrant collection {self.collection_name!r} has a vector size that does not match "
                    f"the configured embedding model ({self.vector_size})."
                )

        for field_name, field_schema in PAYLOAD_INDEXES.items():
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=field_schema,
            )

    def upsert_chunks(self, payloads: Sequence[dict[str, object]], vectors: Sequence[Sequence[float]]) -> None:
        if len(payloads) != len(vectors):
            raise ValueError("Each chunk payload must have exactly one embedding vector.")

        points: list[models.PointStruct] = []
        for payload, vector in zip(payloads, vectors, strict=True):
            self._validate_payload(payload)
            if len(vector) != self.vector_size:
                raise ValueError(
                    f"Embedding vector dimension {len(vector)} does not match collection dimension {self.vector_size}."
                )
            points.append(
                models.PointStruct(
                    id=str(uuid5(NAMESPACE_URL, str(payload["chunk_id"]))),
                    vector=list(vector),
                    payload=payload,
                )
            )

        if points:
            self.client.upsert(collection_name=self.collection_name, points=points, wait=True)

    def search(
        self,
        vector: Sequence[float],
        limit: int = 5,
        query_filter: models.Filter | None = None,
    ) -> list[models.ScoredPoint]:
        if len(vector) != self.vector_size:
            raise ValueError(
                f"Query vector dimension {len(vector)} does not match collection dimension {self.vector_size}."
            )
        if limit <= 0:
            raise ValueError("limit must be greater than zero.")

        response = self.client.query_points(
            collection_name=self.collection_name,
            query=list(vector),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return response.points

    def count_document_chunks(self, document_id: int, tenant_id: int) -> int:
        response = self.client.count(
            collection_name=self.collection_name,
            count_filter=self._document_filter(document_id, tenant_id),
            exact=True,
        )
        return response.count

    def update_document_acl(
        self,
        document_id: int,
        tenant_id: int,
        allowed_roles: Sequence[str],
        allowed_users: Sequence[int],
        classification: str,
    ) -> None:
        if type(document_id) is not int or type(tenant_id) is not int:
            raise ValueError("document_id and tenant_id must be integers.")
        if any(not isinstance(role, str) for role in allowed_roles):
            raise ValueError("allowed_roles must contain only strings.")
        if any(type(user_id) is not int for user_id in allowed_users):
            raise ValueError("allowed_users must contain only integer IDs.")

        self.client.set_payload(
            collection_name=self.collection_name,
            payload={
                "tenant_id": tenant_id,
                "allowed_roles": list(allowed_roles),
                "allowed_users": list(allowed_users),
                "classification": classification,
            },
            points=models.FilterSelector(filter=self._document_filter(document_id, tenant_id)),
            wait=True,
        )

    @staticmethod
    def _document_filter(document_id: int, tenant_id: int) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                ),
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=tenant_id),
                ),
            ]
        )

    @staticmethod
    def _validate_payload(payload: dict[str, object]) -> None:
        if payload.get("tenant_id") is None:
            raise ValueError("Every Qdrant chunk payload must include tenant_id.")
        if set(payload) != PAYLOAD_FIELDS:
            missing = sorted(PAYLOAD_FIELDS - set(payload))
            extra = sorted(set(payload) - PAYLOAD_FIELDS)
            raise ValueError(f"Chunk payload fields are invalid; missing={missing}, extra={extra}.")