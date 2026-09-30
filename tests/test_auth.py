import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from app.core.config import get_settings
from app.db.database import SessionLocal
from app.main import app
from app.models.relational import Role, Tenant, User, UserRole

client = TestClient(app)
settings = get_settings()


def seed_user(username: str, password: str = "SecretPass123!", roles: list[str] | None = None) -> dict[str, object]:
    tenant_name = f"tenant-{uuid.uuid4().hex[:8]}"
    chosen_roles = roles or ["EMPLOYEE"]

    with SessionLocal() as db:
        tenant = Tenant(name=tenant_name)
        db.add(tenant)
        db.flush()

        user = User(
            tenant_id=tenant.id,
            username=username,
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.flush()

        for role_name in chosen_roles:
            role = db.scalar(select(Role).where(Role.name == role_name))
            if role is None:
                role = Role(name=role_name)
                db.add(role)
                db.flush()
            db.add(UserRole(user_id=user.id, role_id=role.id))

        db.commit()

    return {
        "username": username,
        "password": password,
        "tenant_id": tenant.id,
        "user_id": user.id,
        "roles": chosen_roles,
    }


def test_login_success() -> None:
    username = f"alice-{uuid.uuid4().hex[:8]}"
    seed_user(username=username, password="SecretPass123!")

    response = client.post("/auth/login", json={"username": username, "password": "SecretPass123!"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["access_token"]


def test_login_incorrect_password() -> None:
    username = f"bob-{uuid.uuid4().hex[:8]}"
    seed_user(username=username, password="CorrectPass123!")

    response = client.post("/auth/login", json={"username": username, "password": "WrongPass123!"})

    assert response.status_code == 401


def test_login_unknown_user() -> None:
    response = client.post(
        "/auth/login",
        json={"username": f"ghost-{uuid.uuid4().hex[:8]}", "password": "Whatever123!"},
    )

    assert response.status_code == 401


def test_missing_jwt() -> None:
    response = client.get("/protected")

    assert response.status_code == 401


def test_invalid_jwt() -> None:
    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer not-a-valid-jwt"},
    )

    assert response.status_code == 401


def test_expired_jwt() -> None:
    expired_token = jwt.encode(
        {
            "user_id": 999999,
            "tenant_id": 999999,
            "roles": ["EMPLOYEE"],
            "exp": int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert response.status_code == 401


def test_authenticated_user_identity() -> None:
    user = seed_user(username=f"charlie-{uuid.uuid4().hex[:8]}", password="PassWord123!", roles=["EMPLOYEE", "HR"])
    login_response = client.post(
        "/auth/login",
        json={"username": user["username"], "password": user["password"]},
    )

    token = login_response.json()["access_token"]
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == user["username"]
    assert body["tenant_id"] == user["tenant_id"]
    assert set(body["roles"]).issubset(set(user["roles"]))


def test_tenant_id_comes_from_verified_jwt() -> None:
    user = seed_user(username=f"david-{uuid.uuid4().hex[:8]}", password="PassWord123!", roles=["EMPLOYEE"])
    login_response = client.post(
        "/auth/login",
        json={"username": user["username"], "password": user["password"]},
    )
    token = login_response.json()["access_token"]

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
        params={"tenant_id": 999999},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == user["tenant_id"]
