from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class DocumentPermissionUpdateRequest(BaseModel):
    allowed_roles: list[StrictStr] = Field(default_factory=list)
    allowed_users: list[StrictInt] = Field(default_factory=list)
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL"] | None = None

    model_config = ConfigDict(extra="ignore")


class DocumentPermissionUpdateResponse(BaseModel):
    document_id: int
    classification: str
    allowed_role_count: int
    allowed_user_count: int