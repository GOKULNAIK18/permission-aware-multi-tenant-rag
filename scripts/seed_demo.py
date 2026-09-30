"""Seed fictional users/documents for isolated local development only.

The fixed demo passwords are public sample credentials, not production secrets.
Never use these accounts or this seeder in a deployed environment.
"""

from sqlalchemy import select

from app.auth.password import hash_password
from app.db.database import SessionLocal
from app.models.relational import (
    Document,
    DocumentRole,
    DocumentUser,
    Role,
    Tenant,
    User,
    UserRole,
)


def get_or_create_tenant(db, name: str) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.name == name))

    if tenant is None:
        tenant = Tenant(name=name)
        db.add(tenant)
        db.flush()

    return tenant


def get_or_create_role(db, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))

    if role is None:
        role = Role(name=name)
        db.add(role)
        db.flush()

    return role


def get_or_create_user(
    db,
    tenant: Tenant,
    username: str,
    password: str,
) -> User:
    user = db.scalar(select(User).where(User.username == username))

    if user is None:
        user = User(
            tenant_id=tenant.id,
            username=username,
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.flush()

    return user


def add_user_role(db, user: User, role: Role) -> None:
    existing = db.scalar(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role.id,
        )
    )

    if existing is None:
        db.add(UserRole(user_id=user.id, role_id=role.id))


def create_document(
    db,
    tenant: Tenant,
    name: str,
    classification: str,
) -> Document:
    document = db.scalar(
        select(Document).where(
            Document.tenant_id == tenant.id,
            Document.name == name,
        )
    )

    if document is None:
        document = Document(
            tenant_id=tenant.id,
            name=name,
            classification=classification,
        )
        db.add(document)
        db.flush()

    return document


def add_document_role(db, document: Document, role: Role) -> None:
    existing = db.scalar(
        select(DocumentRole).where(
            DocumentRole.document_id == document.id,
            DocumentRole.role_id == role.id,
        )
    )

    if existing is None:
        db.add(
            DocumentRole(
                document_id=document.id,
                role_id=role.id,
            )
        )


def add_document_user(db, document: Document, user: User) -> None:
    existing = db.scalar(
        select(DocumentUser).where(
            DocumentUser.document_id == document.id,
            DocumentUser.user_id == user.id,
        )
    )

    if existing is None:
        db.add(
            DocumentUser(
                document_id=document.id,
                user_id=user.id,
            )
        )


def main() -> None:
    db = SessionLocal()

    try:
        # ---------------------------------------------------------
        # Roles
        # ---------------------------------------------------------
        admin_role = get_or_create_role(db, "ADMIN")
        employee_role = get_or_create_role(db, "EMPLOYEE")

        # ---------------------------------------------------------
        # Tenants
        # ---------------------------------------------------------
        acme = get_or_create_tenant(db, "ACME")
        globex = get_or_create_tenant(db, "Globex")
        wayne = get_or_create_tenant(db, "Wayne")

        # ---------------------------------------------------------
        # Users
        # ---------------------------------------------------------
        alice = get_or_create_user(
            db,
            acme,
            "alice",
            "AlicePass123!",
        )

        bob = get_or_create_user(
            db,
            acme,
            "bob",
            "BobPass123!",
        )

        charlie = get_or_create_user(
            db,
            globex,
            "charlie",
            "CharliePass123!",
        )

        diana = get_or_create_user(
            db,
            wayne,
            "diana",
            "DianaPass123!",
        )

        # ---------------------------------------------------------
        # User roles
        # ---------------------------------------------------------
        add_user_role(db, alice, admin_role)
        add_user_role(db, bob, employee_role)
        add_user_role(db, charlie, employee_role)
        add_user_role(db, diana, employee_role)

        # ---------------------------------------------------------
        # ACME documents
        # ---------------------------------------------------------

        # Accessible by every ACME employee.
        acme_policy = create_document(
            db,
            acme,
            "acme_employee_policy",
            "INTERNAL",
        )
        add_document_role(db, acme_policy, employee_role)

        # Accessible only by ACME admins.
        acme_financial = create_document(
            db,
            acme,
            "acme_financial_report",
            "CONFIDENTIAL",
        )
        add_document_role(db, acme_financial, admin_role)

        # Accessible only by Alice.
        alice_private = create_document(
            db,
            acme,
            "alice_private_document",
            "CONFIDENTIAL",
        )
        add_document_user(db, alice_private, alice)

        # ---------------------------------------------------------
        # Globex document
        # ---------------------------------------------------------
        globex_policy = create_document(
            db,
            globex,
            "globex_employee_policy",
            "INTERNAL",
        )
        add_document_role(db, globex_policy, employee_role)

        # ---------------------------------------------------------
        # Wayne document
        # ---------------------------------------------------------
        wayne_policy = create_document(
            db,
            wayne,
            "wayne_employee_policy",
            "INTERNAL",
        )
        add_document_role(db, wayne_policy, employee_role)

        db.commit()

        print()
        print("Demo data created successfully.")
        print()
        print("Users:")
        print("  ACME")
        print("    alice (ADMIN; local-only demo account)")
        print("    bob (EMPLOYEE; local-only demo account)")
        print()
        print("  Globex")
        print("    charlie / CharliePass123! (EMPLOYEE)")
        print()
        print("  Wayne")
        print("    diana / DianaPass123!   (EMPLOYEE)")
        print()
        print("Documents:")
        print("  ACME employee policy       -> EMPLOYEE")
        print("  ACME financial report      -> ADMIN")
        print("  Alice private document     -> Alice only")
        print("  Globex employee policy     -> Globex EMPLOYEE")
        print("  Wayne employee policy      -> Wayne EMPLOYEE")
        print()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    main()
