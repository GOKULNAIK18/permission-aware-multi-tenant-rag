from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.auth.dependencies import AuthenticatedUser, get_current_user
from app.services.ingestion_service import DocumentIngestionService

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload")
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    classification: Annotated[str, Form()] = "INTERNAL",
    allowed_roles: Annotated[str | None, Form()] = None,
    allowed_users: Annotated[str | None, Form()] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, int | str]:
    if "ADMIN" not in current_user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required.",
        )

    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A document file is required.")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")

    service = DocumentIngestionService()
    result = service.ingest_document_text(
        tenant_id=current_user.tenant_id,
        name=file.filename,
        text=text,
        classification=classification,
        allowed_roles=[role.strip() for role in (allowed_roles or "").split(",") if role.strip()],
        allowed_users=[int(user_id.strip()) for user_id in (allowed_users or "").split(",") if user_id.strip()],
        source_path=file.filename,
    )

    return {
        "document_id": result.document_id,
        "chunk_count": result.chunk_count,
    }
