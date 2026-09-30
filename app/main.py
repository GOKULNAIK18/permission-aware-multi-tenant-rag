from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import AuthenticatedUser, get_current_user
from app.auth.jwt import create_access_token
from app.auth.password import verify_password
from app.api.documents import router as documents_router
from app.api.rag import router as rag_router
from app.api.permissions import router as permissions_router
from app.core.config import get_settings
from app.db.database import get_db
from app.models.relational import Role, Tenant, User, UserRole
from app.schemas.auth import LoginRequest, TokenResponse

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Permission-aware multi-tenant RAG application skeleton.",
    debug=settings.debug,
)

app.include_router(documents_router)
app.include_router(rag_router)
app.include_router(permissions_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"app": settings.app_name, "status": "ok"}


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(User.username == payload.username)
    )

    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    roles = [role.name for role in user.roles]
    access_token = create_access_token(user.id, user.tenant_id, roles)
    return TokenResponse(access_token=access_token, token_type="bearer")


@app.get("/protected")
def protected_route(current_user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "user_id": current_user.user_id,
        "tenant_id": current_user.tenant_id,
        "username": current_user.username,
        "roles": current_user.roles,
    }
