from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.jwt import decode_access_token
from app.db.database import get_db
from app.models.relational import User

security = HTTPBearer(auto_error=False)


class AuthenticatedUser(BaseModel):
    user_id: int
    tenant_id: int
    username: str
    roles: list[str]

    model_config = ConfigDict(from_attributes=True)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_access_token(credentials.credentials)

    user = db.scalar(
        select(User)
        .where(User.id == claims.user_id)
        .where(User.tenant_id == claims.tenant_id)
    )

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_roles = [role.name for role in user.roles]
    if not set(claims.roles).issubset(set(user_roles)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token roles do not match the user",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedUser(
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        roles=user_roles,
    )
