from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import AuthenticatedUser, get_current_user
from app.schemas.permissions import DocumentPermissionUpdateRequest, DocumentPermissionUpdateResponse
from app.services.audit_service import AuditWriteError
from app.services.embedding_service import EmbeddingService
from app.services.permission_service import PermissionManagementError, PermissionManagementService
from app.services.qdrant_service import QdrantVectorStore, create_qdrant_client

router = APIRouter(prefix="/documents", tags=["documents"])


@lru_cache(maxsize=1)
def get_permission_management_service() -> PermissionManagementService:
    embedding_service = EmbeddingService()
    vector_store = QdrantVectorStore(
        client=create_qdrant_client(),
        vector_size=embedding_service.dimension,
    )
    vector_store.ensure_collection()
    return PermissionManagementService(vector_store)


@router.patch("/{document_id}/permissions", response_model=DocumentPermissionUpdateResponse)
def update_document_permissions(
    document_id: int,
    payload: DocumentPermissionUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    permission_service: PermissionManagementService = Depends(get_permission_management_service),
) -> DocumentPermissionUpdateResponse:
    try:
        result = permission_service.update_document_permissions(
            document_id=document_id,
            current_user=current_user,
            allowed_roles=payload.allowed_roles,
            allowed_users=payload.allowed_users,
            classification=payload.classification,
        )
    except AuditWriteError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Required audit logging is unavailable.",
        ) from exc
    except PermissionManagementError as exc:
        details = {
            403: "Administrator role required.",
            404: "Document not found.",
            422: "Requested permissions are invalid.",
            503: "Document permissions could not be synchronized.",
        }
        raise HTTPException(
            status_code=exc.status_code,
            detail=details.get(exc.status_code, "Permission update failed."),
        ) from exc

    return DocumentPermissionUpdateResponse(
        document_id=result.document_id,
        classification=result.classification,
        allowed_role_count=result.allowed_role_count,
        allowed_user_count=result.allowed_user_count,
    )