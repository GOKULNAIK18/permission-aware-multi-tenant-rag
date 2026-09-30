from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.relational import Document, DocumentRole, DocumentUser, Role, Tenant, User
from app.services.embedding_service import EmbeddingService
from app.services.qdrant_service import QdrantVectorStore, create_qdrant_client

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50


@dataclass
class IngestionResult:
    document_id: int
    chunks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    if not isinstance(text, str):
        raise TypeError("Document text must be a string.")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero.")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be zero or greater.")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size.")

    cleaned = text.strip()
    if not cleaned:
        return []

    step = chunk_size - chunk_overlap
    chunks: list[str] = []
    start = 0

    while start < len(cleaned):
        end = min(start + chunk_size, len(cleaned))
        chunk = cleaned[start:end].strip()
        if not chunk:
            break
        chunks.append(chunk)
        if end >= len(cleaned):
            break
        start += step

    return chunks


class DocumentIngestionService:
    """Responsible for loading text, chunking it, and persisting document ACL metadata."""

    def __init__(
        self,
        db_factory: type[Session] | None = None,
        embedding_service: EmbeddingService | None = None,
        vector_store: QdrantVectorStore | None = None,
    ) -> None:
        self.db_factory = db_factory or SessionLocal
        self.embedding_service = embedding_service or EmbeddingService()
        self.vector_store = vector_store

    def ingest_document_text(
        self,
        tenant_id: int | None,
        name: str,
        text: str,
        classification: str,
        allowed_roles: list[str] | None = None,
        allowed_users: list[int] | None = None,
        source_path: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> IngestionResult:
        if tenant_id is None:
            raise ValueError("tenant_id is required for document ingestion.")
        if not name or not name.strip():
            raise ValueError("Document name is required.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Document text cannot be empty.")

        normalized_classification = classification.strip().upper()
        if normalized_classification not in {"PUBLIC", "INTERNAL", "CONFIDENTIAL"}:
            raise ValueError("classification must be one of PUBLIC, INTERNAL, or CONFIDENTIAL.")

        with self.db_factory() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None:
                raise ValueError(f"Tenant {tenant_id} does not exist.")

            document = Document(
                tenant_id=tenant_id,
                name=name.strip(),
                source_path=source_path,
                classification=normalized_classification,
            )
            db.add(document)
            db.flush()

            for role_name in self._normalize_roles(allowed_roles):
                role = db.scalar(select(Role).where(Role.name == role_name))
                if role is None:
                    role = Role(name=role_name)
                    db.add(role)
                    db.flush()
                db.add(DocumentRole(document_id=document.id, role_id=role.id))

            for user_id in self._normalize_user_ids(allowed_users):
                user = db.get(User, user_id)
                if user is None:
                    raise ValueError(f"User {user_id} does not exist.")
                if user.tenant_id != tenant_id:
                    raise ValueError(f"User {user_id} does not belong to tenant {tenant_id}.")
                db.add(DocumentUser(document_id=document.id, user_id=user.id))

            chunks = chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            payloads: list[dict[str, Any]] = []
            for index, chunk in enumerate(chunks):
                payloads.append(
                    {
                        "chunk_id": f"{document.id}-chunk-{index:04d}",
                        "document_id": document.id,
                        "tenant_id": tenant_id,
                        "text": chunk,
                        "allowed_roles": self._normalize_roles(allowed_roles),
                        "allowed_users": self._normalize_user_ids(allowed_users),
                        "classification": normalized_classification,
                    }
                )

            embeddings = self.embedding_service.embed_texts(chunks)
            vector_store = self.vector_store
            if vector_store is None:
                vector_store = QdrantVectorStore(
                    client=create_qdrant_client(),
                    vector_size=self.embedding_service.dimension,
                )
            vector_store.ensure_collection()
            vector_store.upsert_chunks(payloads, embeddings)

            db.commit()
            return IngestionResult(document_id=document.id, chunks=payloads)

    def ingest_document_file(
        self,
        tenant_id: int | None,
        file_path: str | Path,
        classification: str = "INTERNAL",
        allowed_roles: list[str] | None = None,
        allowed_users: list[int] | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> IngestionResult:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Document file not found: {path}")

        text = path.read_text(encoding="utf-8")
        return self.ingest_document_text(
            tenant_id=tenant_id,
            name=path.name,
            text=text,
            classification=classification,
            allowed_roles=allowed_roles,
            allowed_users=allowed_users,
            source_path=str(path),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def ingest_document(self, *args: Any, **kwargs: Any) -> IngestionResult:
        return self.ingest_document_text(*args, **kwargs)

    @staticmethod
    def _normalize_roles(allowed_roles: list[str] | None) -> list[str]:
        if not allowed_roles:
            return []
        roles: list[str] = []
        for role_name in allowed_roles:
            if role_name is None:
                continue
            normalized = str(role_name).strip().upper()
            if normalized and normalized not in roles:
                roles.append(normalized)
        return roles

    @staticmethod
    def _normalize_user_ids(allowed_users: list[int] | None) -> list[int]:
        if not allowed_users:
            return []
        users: list[int] = []
        for user_id in allowed_users:
            normalized = int(user_id)
            if normalized not in users:
                users.append(normalized)
        return users
