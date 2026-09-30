from __future__ import annotations

import pytest

from app.auth.acl import authorize_chunk
from app.auth.dependencies import AuthenticatedUser


@pytest.fixture
def employee() -> AuthenticatedUser:
    return AuthenticatedUser(user_id=1, tenant_id=10, username="employee", roles=["EMPLOYEE"])


def _chunk(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "chunk_id": "test-chunk",
        "document_id": 1,
        "tenant_id": 10,
        "allowed_roles": ["HR"],
        "allowed_users": [2],
        "text": "test content",
        "classification": "INTERNAL",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        {"allowed_roles": ["EMPLOYEE"], "allowed_users": []},
        _chunk(tenant_id=11, allowed_roles=["EMPLOYEE"], allowed_users=[1]),
        _chunk(allowed_roles="EMPLOYEE"),
        _chunk(allowed_roles=[None, 7, {"role": "EMPLOYEE"}]),
        _chunk(allowed_users="1"),
        _chunk(allowed_users=[True]),
        _chunk(allowed_users=["1"]),
        _chunk(allowed_users=[[]]),
        _chunk(allowed_roles=["EMPLOYEE", 7], allowed_users=[1]),
        _chunk(allowed_roles=["EMPLOYEE"], allowed_users=["1"]),
        {"tenant_id": 10, "allowed_roles": ["EMPLOYEE"], "allowed_users": []},
        {
            "tenant_id": 10,
            "allowed_roles": ["EMPLOYEE"],
            "allowed_users": [],
            "chunk_id": "missing-text",
            "document_id": 1,
            "classification": "INTERNAL",
        },
        _chunk(allowed_roles=["HR"], allowed_users=[]),
        _chunk(allowed_roles=[], allowed_users=[2]),
    ],
    ids=[
        "missing-tenant",
        "wrong-tenant",
        "malformed-roles-scalar",
        "malformed-roles-elements",
        "malformed-users-scalar",
        "malformed-users-bool-is-not-user-id",
        "malformed-users-string-id",
        "malformed-users-nested-value",
        "malformed-roles-cannot-fall-back-to-user-grant",
        "malformed-users-cannot-fall-back-to-role-grant",
        "missing-required-chunk-metadata",
        "missing-chunk-text",
        "unauthorized-role-without-user-match",
        "unauthorized-user-without-role-match",
    ],
)
def test_unauthorized_or_malformed_chunk_metadata_fails_closed(
    employee: AuthenticatedUser,
    payload: dict[str, object],
) -> None:
    assert authorize_chunk(employee, payload) is False


def test_matching_role_authorizes_chunk(employee: AuthenticatedUser) -> None:
    assert authorize_chunk(
        employee,
        _chunk(allowed_roles=["EMPLOYEE"], allowed_users=[]),
    ) is True


def test_matching_user_authorizes_chunk_without_role_match(employee: AuthenticatedUser) -> None:
    assert authorize_chunk(
        employee,
        _chunk(allowed_roles=["HR"], allowed_users=[1]),
    ) is True