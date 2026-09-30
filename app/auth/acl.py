from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from qdrant_client import models

from app.auth.dependencies import AuthenticatedUser


def build_acl_filter(current_user: AuthenticatedUser) -> models.Filter:
    """Build a tenant-first Qdrant ACL filter from authenticated identity only."""
    conditions: list[models.Condition] = []
    roles = sorted(set(current_user.roles))
    if roles:
        conditions.append(
            models.FieldCondition(
                key="allowed_roles",
                match=models.MatchAny(any=roles),
            )
        )
    conditions.append(
        models.FieldCondition(
            key="allowed_users",
            match=models.MatchValue(value=current_user.user_id),
        )
    )

    return models.Filter(
        must=[
            models.FieldCondition(
                key="tenant_id",
                match=models.MatchValue(value=current_user.tenant_id),
            )
        ],
        min_should=models.MinShould(conditions=conditions, min_count=1),
    )


def authorize_chunk(current_user: AuthenticatedUser, chunk: Mapping[str, Any] | Any) -> bool:
    """Defense-in-depth check matching the tenant and role/user ACL semantics."""
    payload = chunk.payload if hasattr(chunk, "payload") else chunk
    if not isinstance(payload, Mapping):
        return False

    required_fields = {
        "chunk_id",
        "document_id",
        "tenant_id",
        "text",
        "allowed_roles",
        "allowed_users",
        "classification",
    }
    if set(payload) != required_fields:
        return False

    tenant_id = payload.get("tenant_id")
    if type(tenant_id) is not int or tenant_id <= 0 or tenant_id != current_user.tenant_id:
        return False

    allowed_roles = payload.get("allowed_roles")
    allowed_users = payload.get("allowed_users")
    if not isinstance(allowed_roles, list) or any(
        not isinstance(role, str) or role not in {"ADMIN", "HR", "EMPLOYEE"} for role in allowed_roles
    ):
        return False
    if not isinstance(allowed_users, list) or any(type(user_id) is not int or user_id <= 0 for user_id in allowed_users):
        return False
    if not isinstance(payload.get("chunk_id"), str) or not payload["chunk_id"]:
        return False
    if type(payload.get("document_id")) is not int or payload["document_id"] <= 0:
        return False
    if not isinstance(payload.get("text"), str):
        return False
    classification = payload.get("classification")
    if not isinstance(classification, str) or classification not in {"PUBLIC", "INTERNAL", "CONFIDENTIAL"}:
        return False

    role_match = bool(set(current_user.roles).intersection(allowed_roles))
    user_match = current_user.user_id in allowed_users
    return role_match or user_match