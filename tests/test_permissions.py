from __future__ import annotations

import uuid
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import select

from app.auth.dependencies import AuthenticatedUser
from app.auth.jwt import create_access_token
from app.db.database import SessionLocal
from app.main import app
from app.models.relational import AuditLog, Document, DocumentRole, DocumentUser, Role, Tenant, User, UserRole
from app.services.audit_service import AuditEventType, AuditLoggingService
from app.services.permission_service import (
    DocumentNotFoundError,
    InvalidPermissionsError,
    PermissionDeniedError,
    PermissionManagementService,
    PermissionUpdateFailedError,
)
from app.services.prompt_service import ChatPrompt
from app.services.qdrant_service import QdrantVectorStore
from app.services.rag_service import RAGService
from app.services.semantic_search_service import SemanticSearchService


class StaticEmbeddingService:
    def embed_text(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class CapturingLLM:
    def __init__(self) -> None:
        self.prompts: list[ChatPrompt] = []

    def complete(self, prompt: ChatPrompt) -> str:
        self.prompts.append(prompt)
        return "safe answer"


def _create_principals() -> dict[str, Any]:
    with SessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        tenant_a = Tenant(name=f"permission-a-{suffix}")
        tenant_b = Tenant(name=f"permission-b-{suffix}")
        db.add_all([tenant_a, tenant_b])
        db.flush()

        role_rows: dict[str, Role] = {}
        for role_name in ("ADMIN", "EMPLOYEE", "HR"):
            role = db.scalar(select(Role).where(Role.name == role_name))
            if role is None:
                role = Role(name=role_name)
                db.add(role)
                db.flush()
            role_rows[role_name] = role

        identities: dict[str, User] = {}
        for label, tenant, role_names in (
            ("admin_a", tenant_a, ["ADMIN"]),
            ("alice", tenant_a, ["EMPLOYEE"]),
            ("bob", tenant_a, ["EMPLOYEE"]),
            ("hr", tenant_a, ["HR"]),
            ("admin_b", tenant_b, ["ADMIN"]),
            ("other_tenant_user", tenant_b, ["EMPLOYEE"]),
        ):
            user = User(
                tenant_id=tenant.id,
                username=f"{label}-{suffix}",
                password_hash="test-password-hash",
                is_active=True,
            )
            db.add(user)
            db.flush()
            identities[label] = user
            for role_name in role_names:
                db.add(UserRole(user_id=user.id, role_id=role_rows[role_name].id))

        db.commit()
        return {
            "tenant_a": tenant_a.id,
            "tenant_b": tenant_b.id,
            "users": {key: (user.id, user.tenant_id, [role.name for role in user.roles]) for key, user in identities.items()},
        }


def _create_document(
    principals: dict[str, Any],
    allowed_roles: list[str],
    allowed_users: list[int] | None = None,
    tenant_key: str = "tenant_a",
) -> tuple[int, QdrantVectorStore, dict[str, object]]:
    tenant_id = principals[tenant_key]
    with SessionLocal() as db:
        document = Document(
            tenant_id=tenant_id,
            name=f"fictional-{uuid.uuid4().hex[:6]}.txt",
            source_path="fictional.txt",
            classification="INTERNAL",
        )
        db.add(document)
        db.flush()
        for role_name in allowed_roles:
            role = db.scalar(select(Role).where(Role.name == role_name))
            assert role is not None
            db.add(DocumentRole(document_id=document.id, role_id=role.id))
        for user_id in allowed_users or []:
            db.add(DocumentUser(document_id=document.id, user_id=user_id))
        db.commit()
        document_id = document.id

    vector_store = QdrantVectorStore(QdrantClient(":memory:"), vector_size=3)
    vector_store.ensure_collection()
    payload: dict[str, object] = {
        "chunk_id": f"permission-{document_id}-chunk-0",
        "document_id": document_id,
        "tenant_id": tenant_id,
        "text": f"PRIVATE-CANARY document text {document_id}",
        "allowed_roles": allowed_roles,
        "allowed_users": allowed_users or [],
        "classification": "INTERNAL",
    }
    vector_store.upsert_chunks([payload], [[1.0, 0.0, 0.0]])
    return document_id, vector_store, payload


def _authenticated_user(principals: dict[str, Any], label: str) -> AuthenticatedUser:
    user_id, tenant_id, roles = principals["users"][label]
    return AuthenticatedUser(user_id=user_id, tenant_id=tenant_id, username=label, roles=roles)


def _permission_service(vector_store: QdrantVectorStore) -> PermissionManagementService:
    return PermissionManagementService(vector_store, audit_service=AuditLoggingService())


def _rag(vector_store: QdrantVectorStore) -> tuple[RAGService, CapturingLLM]:
    llm = CapturingLLM()
    retrieval = SemanticSearchService(StaticEmbeddingService(), vector_store)  # type: ignore[arg-type]
    return RAGService(retrieval, llm), llm


def _chunk_ids(result: Any) -> set[str]:
    return {str(chunk.payload["chunk_id"]) for chunk in result.retrieved_chunks}


def _latest_permission_audit(user_id: int, tenant_id: int) -> AuditLog:
    with SessionLocal() as db:
        records = list(db.scalars(
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.id.desc())
        ).all())
        record = next(
            item
            for item in records
            if item.details is not None
            and item.details.get("event_type")
            in {
                AuditEventType.DOCUMENT_PERMISSION_UPDATED.value,
                AuditEventType.DOCUMENT_PERMISSION_UPDATE_FAILED.value,
            }
        )
        assert record is not None
        return record


def test_authorized_admin_updates_postgres_qdrant_and_audit() -> None:
    principals = _create_principals()
    alice_id = principals["users"]["alice"][0]
    document_id, vector_store, payload = _create_document(principals, ["ADMIN"])
    admin = _authenticated_user(principals, "admin_a")

    result = _permission_service(vector_store).update_document_permissions(
        document_id,
        admin,
        ["EMPLOYEE"],
        [alice_id],
        "CONFIDENTIAL",
    )

    stored = vector_store.client.retrieve(
        vector_store.collection_name,
        ids=[str(uuid5(NAMESPACE_URL, str(payload["chunk_id"])))],
        with_payload=True,
    )[0]
    assert result.document_id == document_id
    assert stored.payload is not None
    assert stored.payload["tenant_id"] == admin.tenant_id
    assert stored.payload["allowed_roles"] == ["EMPLOYEE"]
    assert stored.payload["allowed_users"] == [alice_id]
    assert stored.payload["classification"] == "CONFIDENTIAL"

    with SessionLocal() as db:
        document = db.get(Document, document_id)
        assert document is not None
        assert document.classification == "CONFIDENTIAL"
        assert [link.role.name for link in document.allowed_roles] == ["EMPLOYEE"]
        assert [link.user_id for link in document.allowed_users] == [alice_id]

    audit = _latest_permission_audit(admin.user_id, admin.tenant_id)
    assert audit.success is True
    assert audit.details["event_type"] == "document.permission.updated"
    assert audit.details["document_id"] == document_id
    assert "PRIVATE-CANARY" not in str(audit.details)
    assert audit.query == "[REDACTED]"


def test_non_admin_cannot_change_permissions_and_failure_is_audited() -> None:
    principals = _create_principals()
    document_id, vector_store, _ = _create_document(principals, ["ADMIN"])
    employee = _authenticated_user(principals, "alice")

    with pytest.raises(PermissionDeniedError):
        _permission_service(vector_store).update_document_permissions(document_id, employee, [], [])

    audit = _latest_permission_audit(employee.user_id, employee.tenant_id)
    assert audit.success is False
    assert audit.details["failure_category"] == "permission_denied"


def test_cross_tenant_admin_cannot_modify_document_or_leak_contents() -> None:
    principals = _create_principals()
    document_id, vector_store, payload = _create_document(principals, ["ADMIN"], tenant_key="tenant_b")
    admin_a = _authenticated_user(principals, "admin_a")

    with pytest.raises(DocumentNotFoundError):
        _permission_service(vector_store).update_document_permissions(document_id, admin_a, ["EMPLOYEE"], [])

    point = vector_store.client.scroll(vector_store.collection_name, limit=1, with_payload=True)[0][0]
    assert point.payload == payload
    audit = _latest_permission_audit(admin_a.user_id, admin_a.tenant_id)
    assert audit.success is False
    assert audit.details["failure_category"] == "document_not_found"
    assert "PRIVATE-CANARY" not in str(audit.details)


@pytest.mark.parametrize(
    ("roles", "users"),
    [
        ([True], []),
        ([1], []),
        ("EMPLOYEE", []),
        (["employee"], []),
        ([], [True]),
        ([], ["1"]),
        ([], 1),
    ],
)
def test_invalid_acl_types_are_rejected(roles: Any, users: Any) -> None:
    principals = _create_principals()
    document_id, vector_store, _ = _create_document(principals, ["ADMIN"])
    admin = _authenticated_user(principals, "admin_a")

    with pytest.raises(InvalidPermissionsError):
        _permission_service(vector_store).update_document_permissions(document_id, admin, roles, users)


def test_revocation_takes_effect_on_next_rag_query_without_restarting() -> None:
    principals = _create_principals()
    bob_id = principals["users"]["bob"][0]
    document_id, vector_store, _ = _create_document(principals, ["EMPLOYEE"])
    bob = _authenticated_user(principals, "bob")
    admin = _authenticated_user(principals, "admin_a")
    rag_service, llm = _rag(vector_store)

    before = rag_service.generate("retrieve document", current_user=bob)
    assert _chunk_ids(before) == {f"permission-{document_id}-chunk-0"}
    assert "PRIVATE-CANARY" in llm.prompts[-1].user_message

    _permission_service(vector_store).update_document_permissions(document_id, admin, [], [])
    llm.prompts.clear()
    after = rag_service.generate("retrieve document", current_user=bob)

    assert _chunk_ids(after) == set()
    assert after.answer == "No authorized documents were found."
    assert llm.prompts == []


def test_user_grant_takes_effect_on_next_rag_query() -> None:
    principals = _create_principals()
    alice_id = principals["users"]["alice"][0]
    document_id, vector_store, _ = _create_document(principals, ["ADMIN"])
    alice = _authenticated_user(principals, "alice")
    admin = _authenticated_user(principals, "admin_a")
    rag_service, llm = _rag(vector_store)

    before = rag_service.generate("retrieve document", current_user=alice)
    assert _chunk_ids(before) == set()

    _permission_service(vector_store).update_document_permissions(document_id, admin, [], [alice_id])
    after = rag_service.generate("retrieve document", current_user=alice)

    assert _chunk_ids(after) == {f"permission-{document_id}-chunk-0"}
    assert "PRIVATE-CANARY" in llm.prompts[-1].user_message


@pytest.mark.parametrize(
    ("before_roles", "after_roles", "user_label", "expected_count"),
    [(["HR"], [], "hr", 0), ([], ["HR"], "hr", 1)],
)
def test_role_revocation_and_grant_take_effect_immediately(
    before_roles: list[str],
    after_roles: list[str],
    user_label: str,
    expected_count: int,
) -> None:
    principals = _create_principals()
    document_id, vector_store, _ = _create_document(principals, before_roles)
    user = _authenticated_user(principals, user_label)
    admin = _authenticated_user(principals, "admin_a")
    rag_service, _ = _rag(vector_store)

    before = rag_service.generate("retrieve document", current_user=user)
    _permission_service(vector_store).update_document_permissions(document_id, admin, after_roles, [])
    after = rag_service.generate("retrieve document", current_user=user)

    assert len(before.retrieved_chunks) == (1 if before_roles else 0)
    assert len(after.retrieved_chunks) == expected_count


def test_qdrant_final_update_failure_is_audited_and_leaves_vectors_denied() -> None:
    principals = _create_principals()
    alice_id = principals["users"]["alice"][0]
    document_id, vector_store, _ = _create_document(principals, ["ADMIN"])
    admin = _authenticated_user(principals, "admin_a")
    alice = _authenticated_user(principals, "alice")

    class FailOnFinalUpdate:
        def __init__(self, delegate: QdrantVectorStore) -> None:
            self.delegate = delegate
            self.calls = 0

        def count_document_chunks(self, *args: Any, **kwargs: Any) -> int:
            return self.delegate.count_document_chunks(*args, **kwargs)

        def update_document_acl(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated Qdrant outage")
            self.delegate.update_document_acl(*args, **kwargs)

    failing_service = PermissionManagementService(
        FailOnFinalUpdate(vector_store),  # type: ignore[arg-type]
        audit_service=AuditLoggingService(),
    )
    with pytest.raises(PermissionUpdateFailedError):
        failing_service.update_document_permissions(document_id, admin, [], [alice_id])

    retrieved = _rag(vector_store)[0].generate("retrieve document", current_user=alice)
    assert retrieved.retrieved_chunks == []
    point = vector_store.client.scroll(vector_store.collection_name, limit=1, with_payload=True)[0][0]
    assert point.payload is not None
    assert point.payload["allowed_roles"] == []
    assert point.payload["allowed_users"] == []
    audit = _latest_permission_audit(admin.user_id, admin.tenant_id)
    assert audit.success is False
    assert audit.details["failure_category"] == "qdrant_update_failed"
    assert "PRIVATE-CANARY" not in str(audit.details)


def test_permission_endpoint_uses_jwt_identity_and_ignores_body_identity() -> None:
    principals = _create_principals()
    document_id, vector_store, _ = _create_document(principals, ["ADMIN"])
    admin = _authenticated_user(principals, "admin_a")
    other_tenant = principals["tenant_b"]
    token = create_access_token(admin.user_id, admin.tenant_id, admin.roles)
    service = _permission_service(vector_store)

    from app.api.permissions import get_permission_management_service

    app.dependency_overrides[get_permission_management_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.patch(
                f"/documents/{document_id}/permissions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "allowed_roles": ["EMPLOYEE"],
                    "allowed_users": [],
                    "tenant_id": other_tenant,
                    "user_id": 999999,
                },
            )
    finally:
        app.dependency_overrides.pop(get_permission_management_service, None)

    assert response.status_code == 200
    audit = _latest_permission_audit(admin.user_id, admin.tenant_id)
    assert audit.user_id == admin.user_id
    assert audit.tenant_id == admin.tenant_id
    assert audit.details["event_type"] == "document.permission.updated"


def test_permission_endpoint_rejects_strict_malformed_acl_values() -> None:
    principals = _create_principals()
    document_id, vector_store, _ = _create_document(principals, ["ADMIN"])
    admin = _authenticated_user(principals, "admin_a")
    token = create_access_token(admin.user_id, admin.tenant_id, admin.roles)
    service = _permission_service(vector_store)

    from app.api.permissions import get_permission_management_service

    app.dependency_overrides[get_permission_management_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.patch(
                f"/documents/{document_id}/permissions",
                headers={"Authorization": f"Bearer {token}"},
                json={"allowed_roles": ["EMPLOYEE"], "allowed_users": [True]},
            )
    finally:
        app.dependency_overrides.pop(get_permission_management_service, None)

    assert response.status_code == 422