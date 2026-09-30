from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from sqlalchemy.orm import RelationshipProperty


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    users: Mapped[list["User"]] = relationship(back_populates="tenant")
    documents: Mapped[list["Document"]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    roles: Mapped[list["Role"]] = relationship(secondary="user_roles", back_populates="users")
    document_links: Mapped[list["DocumentUser"]] = relationship(back_populates="user")

    __table_args__ = (
        Index("ix_users_tenant_username", "tenant_id", "username", unique=True),
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)

    users: Mapped[list["User"]] = relationship(secondary="user_roles", back_populates="roles")
    document_links: Mapped[list["DocumentRole"]] = relationship(back_populates="role")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True, index=True)

    __table_args__ = (
        Index("ix_user_roles_role_user", "role_id", "user_id", unique=True),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    classification: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="documents")
    allowed_roles: Mapped[list["DocumentRole"]] = relationship(back_populates="document")
    allowed_users: Mapped[list["DocumentUser"]] = relationship(back_populates="document")

    __table_args__ = (
        Index("ix_documents_tenant_name", "tenant_id", "name"),
    )


class DocumentRole(Base):
    __tablename__ = "document_roles"

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), primary_key=True, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True, index=True)

    document: Mapped[Document] = relationship(back_populates="allowed_roles")
    role: Mapped[Role] = relationship(back_populates="document_links")

    __table_args__ = (
        Index("ix_document_roles_role_document", "role_id", "document_id", unique=True),
    )


class DocumentUser(Base):
    __tablename__ = "document_users"

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)

    document: Mapped[Document] = relationship(back_populates="allowed_users")
    user: Mapped[User] = relationship(back_populates="document_links")

    __table_args__ = (
        Index("ix_document_users_user_document", "user_id", "document_id", unique=True),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    document_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    chunk_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    details: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_logs_tenant_user_time", "tenant_id", "user_id", "timestamp"),
    )
