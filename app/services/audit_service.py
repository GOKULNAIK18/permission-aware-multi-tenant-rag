from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedUser
from app.db.database import SessionLocal
from app.models.relational import AuditLog

logger = logging.getLogger(__name__)
REDACTED_QUERY = "[REDACTED]"


class AuditEventType(StrEnum):
    RAG_RETRIEVAL_SUCCEEDED = "rag.retrieval.succeeded"
    RAG_RETRIEVAL_FAILED = "rag.retrieval.failed"
    RAG_AUTHORIZATION_FAILED = "rag.authorization.failed"
    DOCUMENT_PERMISSION_UPDATED = "document.permission.updated"
    DOCUMENT_PERMISSION_UPDATE_FAILED = "document.permission.update_failed"


class AuditWriteError(RuntimeError):
    """Raised when a security-relevant audit event cannot be committed."""


@dataclass(frozen=True)
class AuditEvent:
    request_id: str
    event_type: AuditEventType
    user_id: int
    tenant_id: int
    result_count: int
    success: bool
    failure_category: str | None = None


class AuditLoggingService:
    """Persist metadata-only audit events using the authenticated identity."""

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def record_rag_event(
        self,
        current_user: AuthenticatedUser,
        event_type: AuditEventType,
        result_count: int,
        failure_category: str | None = None,
        request_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            request_id=request_id or str(uuid.uuid4()),
            event_type=event_type,
            user_id=current_user.user_id,
            tenant_id=current_user.tenant_id,
            result_count=max(0, result_count),
            success=event_type is AuditEventType.RAG_RETRIEVAL_SUCCEEDED,
            failure_category=self._safe_failure_category(failure_category),
        )
        details: dict[str, str | int] = {
            "event_type": event.event_type.value,
            "result_count": event.result_count,
        }
        if event.failure_category is not None:
            details["failure_category"] = event.failure_category

        try:
            with self.session_factory() as db:
                db.add(
                    AuditLog(
                        request_id=event.request_id,
                        user_id=event.user_id,
                        tenant_id=event.tenant_id,
                        query=REDACTED_QUERY,
                        document_ids=None,
                        chunk_ids=None,
                        model=None,
                        success=event.success,
                        details=details,
                        error=event.failure_category,
                    )
                )
                db.commit()
        except Exception as exc:
            logger.error(
                "Audit write failed event_type=%s user_id=%s tenant_id=%s request_id=%s",
                event.event_type.value,
                event.user_id,
                event.tenant_id,
                event.request_id,
            )
            raise AuditWriteError("Unable to persist the required audit event.") from exc

        return event

    def record_permission_event(
        self,
        current_user: AuthenticatedUser,
        event_type: AuditEventType,
        document_id: int | None,
        allowed_role_count: int,
        allowed_user_count: int,
        classification: str | None = None,
        failure_category: str | None = None,
        request_id: str | None = None,
    ) -> AuditEvent:
        if event_type not in {
            AuditEventType.DOCUMENT_PERMISSION_UPDATED,
            AuditEventType.DOCUMENT_PERMISSION_UPDATE_FAILED,
        }:
            raise ValueError("Permission events require a document permission event type.")

        event = AuditEvent(
            request_id=request_id or str(uuid.uuid4()),
            event_type=event_type,
            user_id=current_user.user_id,
            tenant_id=current_user.tenant_id,
            result_count=max(0, allowed_user_count + allowed_role_count),
            success=event_type is AuditEventType.DOCUMENT_PERMISSION_UPDATED,
            failure_category=self._safe_failure_category(failure_category),
        )
        details: dict[str, str | int | None] = {
            "event_type": event.event_type.value,
            "document_id": document_id,
            "allowed_role_count": max(0, allowed_role_count),
            "allowed_user_count": max(0, allowed_user_count),
        }
        if classification in {"PUBLIC", "INTERNAL", "CONFIDENTIAL"}:
            details["classification"] = classification
        if event.failure_category is not None:
            details["failure_category"] = event.failure_category

        self._persist_event(event, details)
        return event

    def _persist_event(self, event: AuditEvent, details: dict[str, str | int | None]) -> None:
        try:
            with self.session_factory() as db:
                db.add(
                    AuditLog(
                        request_id=event.request_id,
                        user_id=event.user_id,
                        tenant_id=event.tenant_id,
                        query=REDACTED_QUERY,
                        document_ids=None,
                        chunk_ids=None,
                        model=None,
                        success=event.success,
                        details=details,
                        error=event.failure_category,
                    )
                )
                db.commit()
        except Exception as exc:
            logger.error(
                "Audit write failed event_type=%s user_id=%s tenant_id=%s request_id=%s",
                event.event_type.value,
                event.user_id,
                event.tenant_id,
                event.request_id,
            )
            raise AuditWriteError("Unable to persist the required audit event.") from exc

    @staticmethod
    def _safe_failure_category(category: str | None) -> str | None:
        if category is None:
            return None
        allowed_categories = {
            "authorization_boundary_violation",
            "retrieval_failure",
            "permission_denied",
            "document_not_found",
            "invalid_acl",
            "qdrant_update_failed",
            "database_update_failed",
        }
        return category if category in allowed_categories else "security_failure"
