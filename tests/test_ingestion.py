import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import select

from app.api import documents as documents_api
from app.auth.jwt import create_access_token
from app.db.database import SessionLocal
from app.main import app
from app.models.relational import Document, Role, Tenant, User, UserRole
from app.services.embedding_service import EmbeddingService
from app.services.ingestion_service import DocumentIngestionService, chunk_text
from app.services.qdrant_service import QdrantVectorStore


class FakeSentenceTransformer:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def get_sentence_embedding_dimension(self) -> int:
        return 3

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        return [[float(bool(text.strip())), 0.0, 1.0] for text in texts]


def _ingestion_service() -> DocumentIngestionService:
    embeddings = EmbeddingService(model_name="test-model", model_factory=FakeSentenceTransformer)
    vectors = QdrantVectorStore(QdrantClient(":memory:"), vector_size=embeddings.dimension)
    return DocumentIngestionService(embedding_service=embeddings, vector_store=vectors)


def _create_tenant_and_user(username: str, roles: list[str] | None = None) -> tuple[int, int]:
    unique_username = f"{username}-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        tenant = Tenant(name=f"tenant-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()

        user = User(
            tenant_id=tenant.id,
            username=unique_username,
            password_hash="hashed-value",
            is_active=True,
        )
        db.add(user)
        db.flush()

        for role_name in roles or ["EMPLOYEE"]:
            role = db.scalar(select(Role).where(Role.name == role_name))
            if role is None:
                role = Role(name=role_name)
                db.add(role)
                db.flush()
            db.add(UserRole(user_id=user.id, role_id=role.id))

        db.commit()
        return tenant.id, user.id


def test_chunk_text_creates_overlapping_chunks() -> None:
    text = "This is a sample document. " * 40
    chunks = chunk_text(text, chunk_size=120, chunk_overlap=20)

    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert chunks[0] == text[:120]


def test_ingestion_registers_document_acl_and_chunk_metadata() -> None:
    tenant_id, user_id = _create_tenant_and_user("alice_ingest", ["HR", "ADMIN"])
    service = _ingestion_service()

    result = service.ingest_document_text(
        tenant_id=tenant_id,
        name="leave_policy.txt",
        text=("HR leave policy. " * 80),
        classification="INTERNAL",
        allowed_roles=["HR", "ADMIN"],
        allowed_users=[user_id],
        chunk_size=180,
        chunk_overlap=30,
    )

    assert result.document_id is not None
    assert result.chunk_count > 0
    assert result.chunks
    assert all(chunk["tenant_id"] == tenant_id for chunk in result.chunks)
    assert all(chunk["document_id"] == result.document_id for chunk in result.chunks)
    assert all(chunk["classification"] == "INTERNAL" for chunk in result.chunks)

    with SessionLocal() as db:
        document = db.get(Document, result.document_id)
        assert document is not None
        assert document.name == "leave_policy.txt"
        assert document.tenant_id == tenant_id
        assert document.classification == "INTERNAL"
        assert len(document.allowed_roles) == 2
        assert len(document.allowed_users) == 1


def test_ingestion_rejects_missing_tenant_id() -> None:
    service = _ingestion_service()

    with pytest.raises(ValueError, match="tenant_id"):
        service.ingest_document_text(
            tenant_id=None,
            name="missing-tenant.txt",
            text="This should fail before insertion.",
            classification="CONFIDENTIAL",
        )


def test_document_upload_requires_admin_and_uses_jwt_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    employee_tenant_id, employee_user_id = _create_tenant_and_user("upload_employee", ["EMPLOYEE"])
    admin_tenant_id, admin_user_id = _create_tenant_and_user("upload_admin", ["ADMIN"])
    captured: dict[str, Any] = {}

    class FakeIngestionService:
        def ingest_document_text(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return type("Result", (), {"document_id": 812, "chunk_count": 1})()

    monkeypatch.setattr(documents_api, "DocumentIngestionService", lambda: FakeIngestionService())
    client = TestClient(app)
    upload = {"file": ("policy.txt", b"Fictional policy text.", "text/plain")}

    unauthenticated = client.post("/documents/upload", files=upload)
    assert unauthenticated.status_code == 401

    employee_token = create_access_token(employee_user_id, employee_tenant_id, ["EMPLOYEE"])

    employee_response = client.post(
        "/documents/upload",
        headers={"Authorization": f"Bearer {employee_token}"},
        files=upload,
    )
    assert employee_response.status_code == 403

    admin_token = create_access_token(admin_user_id, admin_tenant_id, ["ADMIN"])
    admin_response = client.post(
        "/documents/upload",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"tenant_id": str(employee_tenant_id), "classification": "INTERNAL"},
        files=upload,
    )

    assert admin_response.status_code == 200
    assert admin_response.json() == {"document_id": 812, "chunk_count": 1}
    assert captured["tenant_id"] == admin_tenant_id
