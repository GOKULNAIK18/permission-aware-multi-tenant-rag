from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings

settings = get_settings()


class TokenClaims(BaseModel):
    user_id: int
    tenant_id: int
    roles: list[str]
    exp: int

    model_config = ConfigDict(extra="ignore")


def create_access_token(user_id: int, tenant_id: int, roles: list[str]) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "roles": roles,
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["user_id", "tenant_id", "roles", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc

    try:
        return TokenClaims.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token claims are invalid",
        ) from exc


def get_token_expiration() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
