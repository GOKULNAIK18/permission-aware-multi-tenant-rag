from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.dependencies import AuthenticatedUser
from app.db.database import SessionLocal
from app.models.relational import AuditLog, Tenant, User
from app.services.audit_service import AuditEventType, AuditLoggingService, AuditWriteError


def _create_authenticated_user() -> AuthenticatedUser:
    with SessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        tenant = Tenant(name=f"audit-tenant-{suffix}")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            username=f"audit-user-{suffix}",
            password_hash="not-a-real-password-hash",
            is_active=True,
        )
        db.add(user)
        db.commit()
        return AuthenticatedUser(
            user_id=user.id,
            tenant_id=tenant.id,
            username=user.username,
            roles=[],
        )


def test_audit_write_failure_is_explicit() -> None:
    class BrokenSession:
        def __enter__(self) -> BrokenSession:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def add(self, _: Any) -> None:
            raise RuntimeError("database unavailable")

    user = AuthenticatedUser(user_id=1, tenant_id=2, username="audit-user", roles=[])
    audit = AuditLoggingService(session_factory=lambda: BrokenSession())  # type: ignore[arg-type]

    with pytest.raises(AuditWriteError, match="required audit event"):
        audit.record_rag_event(
            current_user=user,
            event_type=AuditEventType.RAG_RETRIEVAL_SUCCEEDED,
            result_count=0,
        )


def test_audit_events_store_only_safe_metadata() -> None:
    current_user = _create_authenticated_user()
    request_id = str(uuid.uuid4())
    audit = AuditLoggingService()

    event = audit.record_rag_event(
        current_user=current_user,
        event_type=AuditEventType.RAG_RETRIEVAL_SUCCEEDED,
        result_count=2,
        request_id=request_id,
    )

    with SessionLocal() as db:
        record = db.scalar(select(AuditLog).where(AuditLog.request_id == request_id))

    assert record is not None
    assert event.request_id == record.request_id
    assert record.user_id == current_user.user_id
    assert record.tenant_id == current_user.tenant_id
    assert record.success is True
    assert record.details == {
        "event_type": "rag.retrieval.succeeded",
        "result_count": 2,
    }
    assert record.query == "[REDACTED]"
    assert record.document_ids is None
    assert record.chunk_ids is None
    assert record.model is None
    assert record.error is None
    stored_values = " ".join(
        [record.query, str(record.details), str(record.error), str(record.document_ids), str(record.chunk_ids)]
    )
    for sensitive_value in ("prompt", "LLM response", "JWT", "password", "retrieved chunk text"):
        assert sensitive_value not in stored_values


def test_audit_failure_event_uses_authenticated_identity_and_safe_category() -> None:
    current_user = _create_authenticated_user()
    request_id = str(uuid.uuid4())
    audit = AuditLoggingService()

    audit.record_rag_event(
        current_user=current_user,
        event_type=AuditEventType.RAG_AUTHORIZATION_FAILED,
        result_count=1,
        failure_category="authorization_boundary_violation",
        request_id=request_id,
    )

    with SessionLocal() as db:
        record = db.scalar(select(AuditLog).where(AuditLog.request_id == request_id))

    assert record is not None
    assert record.user_id == current_user.user_id
    assert record.tenant_id == current_user.tenant_id
    assert record.success is False
    assert record.details == {
        "event_type": "rag.authorization.failed",
        "result_count": 1,
        "failure_category": "authorization_boundary_violation",
    }