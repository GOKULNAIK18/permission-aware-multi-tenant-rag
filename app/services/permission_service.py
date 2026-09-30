from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedUser
from app.db.database import SessionLocal
from app.models.relational import Document, DocumentRole, DocumentUser, Role, User
from app.services.audit_service import AuditEventType, AuditLoggingService
from app.services.qdrant_service import QdrantVectorStore

logger = logging.getLogger(__name__)
CLASSIFICATIONS = frozenset({"PUBLIC", "INTERNAL", "CONFIDENTIAL"})


class PermissionManagementError(RuntimeError):
    status_code = 503
    failure_category = "database_update_failed"


class PermissionDeniedError(PermissionManagementError):
    status_code = 403
    failure_category = "permission_denied"


class DocumentNotFoundError(PermissionManagementError):
    status_code = 404
    failure_category = "document_not_found"


class InvalidPermissionsError(PermissionManagementError):
    status_code = 422
    failure_category = "invalid_acl"


class PermissionUpdateFailedError(PermissionManagementError):
    status_code = 503
    failure_category = "qdrant_update_failed"


@dataclass(frozen=True)
class PermissionUpdateResult:
    document_id: int
    classification: str
    allowed_role_count: int
    allowed_user_count: int


class PermissionManagementService:
    def __init__(
        self,
        vector_store: QdrantVectorStore,
        session_factory: Callable[[], Session] = SessionLocal,
        audit_service: AuditLoggingService | None = None,
    ) -> None:
        self.vector_store = vector_store
        self.session_factory = session_factory
        self.audit_service = audit_service or AuditLoggingService(session_factory)

    def update_document_permissions(
        self,
        document_id: int,
        current_user: AuthenticatedUser,
        allowed_roles: Sequence[str],
        allowed_users: Sequence[int],
        classification: str | None = None,
    ) -> PermissionUpdateResult:
        request_id = str(uuid.uuid4())
        safe_role_count = len(allowed_roles) if isinstance(allowed_roles, (list, tuple)) else 0
        safe_user_count = len(allowed_users) if isinstance(allowed_users, (list, tuple)) else 0

        if "ADMIN" not in current_user.roles:
            self._record_failure(
                current_user,
                document_id,
                safe_role_count,
                safe_user_count,
                "permission_denied",
                request_id,
            )
            raise PermissionDeniedError("An administrator role is required to change document permissions.")

        try:
            normalized_roles, normalized_users, normalized_classification = self._validate_request(
                allowed_roles,
                allowed_users,
                classification,
            )
        except InvalidPermissionsError:
            self._record_failure(
                current_user,
                document_id,
                safe_role_count,
                safe_user_count,
                "invalid_acl",
                request_id,
            )
            raise

        stage = "database_update_failed"
        try:
            with self.session_factory() as db:
                document = db.scalar(
                    select(Document)
                    .where(Document.id == document_id)
                    .where(Document.tenant_id == current_user.tenant_id)
                    .with_for_update()
                )
                if document is None:
                    raise DocumentNotFoundError("Document was not found in the authenticated tenant.")

                role_records = list(db.scalars(select(Role).where(Role.name.in_(normalized_roles))).all())
                roles_by_name = {role.name: role for role in role_records}
                if set(roles_by_name) != set(normalized_roles):
                    raise InvalidPermissionsError("One or more requested roles are not configured.")

                user_records = list(
                    db.scalars(
                        select(User)
                        .where(User.id.in_(normalized_users))
                        .where(User.tenant_id == current_user.tenant_id)
                    ).all()
                )
                users_by_id = {user.id: user for user in user_records}
                if set(users_by_id) != set(normalized_users):
                    raise InvalidPermissionsError("Allowed users must exist in the authenticated tenant.")

                final_classification = normalized_classification or document.classification
                stage = "qdrant_update_failed"
                if self.vector_store.count_document_chunks(document.id, current_user.tenant_id) == 0:
                    raise PermissionUpdateFailedError("Document vector payloads are unavailable for update.")

                self.vector_store.update_document_acl(
                    document_id=document.id,
                    tenant_id=current_user.tenant_id,
                    allowed_roles=[],
                    allowed_users=[],
                    classification=document.classification,
                )

                stage = "database_update_failed"
                db.execute(delete(DocumentRole).where(DocumentRole.document_id == document.id))
                db.execute(delete(DocumentUser).where(DocumentUser.document_id == document.id))
                db.add_all(
                    [
                        DocumentRole(document_id=document.id, role_id=roles_by_name[role_name].id)
                        for role_name in normalized_roles
                    ]
                )
                db.add_all(
                    [
                        DocumentUser(document_id=document.id, user_id=users_by_id[user_id].id)
                        for user_id in normalized_users
                    ]
                )
                document.classification = final_classification
                db.commit()

            stage = "qdrant_update_failed"
            self.vector_store.update_document_acl(
                document_id=document_id,
                tenant_id=current_user.tenant_id,
                allowed_roles=normalized_roles,
                allowed_users=normalized_users,
                classification=final_classification,
            )
        except PermissionManagementError as exc:
            self._record_failure(
                current_user,
                document_id,
                len(normalized_roles),
                len(normalized_users),
                exc.failure_category,
                request_id,
                final_classification if "final_classification" in locals() else classification,
            )
            raise
        except Exception as exc:
            logger.error(
                "Document permission update failed stage=%s actor_user_id=%s tenant_id=%s request_id=%s",
                stage,
                current_user.user_id,
                current_user.tenant_id,
                request_id,
            )
            if stage == "qdrant_update_failed" and "final_classification" in locals():
                try:
                    self.vector_store.update_document_acl(
                        document_id=document_id,
                        tenant_id=current_user.tenant_id,
                        allowed_roles=[],
                        allowed_users=[],
                        classification=final_classification,
                    )
                except Exception:
                    logger.error(
                        "Document permission fail-closed Qdrant update also failed document_id=%s tenant_id=%s request_id=%s",
                        document_id,
                        current_user.tenant_id,
                        request_id,
                    )
            self._record_failure(
                current_user,
                document_id,
                len(normalized_roles),
                len(normalized_users),
                stage,
                request_id,
                final_classification if "final_classification" in locals() else classification,
            )
            raise PermissionUpdateFailedError("Document permissions could not be synchronized.") from exc

        self.audit_service.record_permission_event(
            current_user=current_user,
            event_type=AuditEventType.DOCUMENT_PERMISSION_UPDATED,
            document_id=document_id,
            allowed_role_count=len(normalized_roles),
            allowed_user_count=len(normalized_users),
            classification=final_classification,
            request_id=request_id,
        )
        return PermissionUpdateResult(
            document_id=document_id,
            classification=final_classification,
            allowed_role_count=len(normalized_roles),
            allowed_user_count=len(normalized_users),
        )

    def _record_failure(
        self,
        current_user: AuthenticatedUser,
        document_id: int,
        allowed_role_count: int,
        allowed_user_count: int,
        category: str,
        request_id: str,
        classification: str | None = None,
    ) -> None:
        self.audit_service.record_permission_event(
            current_user=current_user,
            event_type=AuditEventType.DOCUMENT_PERMISSION_UPDATE_FAILED,
            document_id=document_id,
            allowed_role_count=allowed_role_count,
            allowed_user_count=allowed_user_count,
            classification=classification,
            failure_category=category,
            request_id=request_id,
        )

    @staticmethod
    def _validate_request(
        allowed_roles: Sequence[str],
        allowed_users: Sequence[int],
        classification: str | None,
    ) -> tuple[list[str], list[int], str | None]:
        if not isinstance(allowed_roles, list) or any(not isinstance(role, str) for role in allowed_roles):
            raise InvalidPermissionsError("allowed_roles must be a list of role names.")
        if not isinstance(allowed_users, list) or any(type(user_id) is not int for user_id in allowed_users):
            raise InvalidPermissionsError("allowed_users must be a list of integer user IDs.")
        if len(set(allowed_roles)) != len(allowed_roles) or len(set(allowed_users)) != len(allowed_users):
            raise InvalidPermissionsError("Permission lists must not contain duplicates.")
        if any(not role or role != role.strip().upper() for role in allowed_roles):
            raise InvalidPermissionsError("Role names must use their configured uppercase form.")
        if classification is not None and classification not in CLASSIFICATIONS:
            raise InvalidPermissionsError("Classification is invalid.")
        return list(allowed_roles), list(allowed_users), classification