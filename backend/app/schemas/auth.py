"""Auth 端点的请求/响应模型（Phase 8a，06 §2.1 形状的唯一定义源）。

入参一律 `extra="forbid"` 锁接缝（与 Phase 7 DecisionIn 同款）：
多塞的键不是宽容而是攻击面——测试接缝、内部字段绝不允许从 HTTP 进来。
password 上限 72：bcrypt 超长截断是**静默**的，8~72 之外宁可 422 也不做「只校验前 72 字节」的暗规。
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field(min_length=1, max_length=100)
    organization_name: str = Field(min_length=1, max_length=200)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str


class LogoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str


class ProfileUpdateIn(BaseModel):
    """PATCH /auth/me 的入参。**字段白名单只有 full_name** ——
    role / organization_id / is_active 一概不接受（针：多传被 403/422 之前先被 extra=forbid 挡，
    但 service 层再挡一次，两道都留着是因为"白名单只在一处"是这类事故的定义性特征）。"""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=100)


class ChangePasswordIn(BaseModel):
    """改口令入参。新口令长度闸与 register 同档（8~72）。"""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


class UserOut(BaseModel):
    """06 §2.1 /auth/me 响应形状的唯一定义源，register/login 复用。"""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    organization_id: uuid.UUID
    created_at: datetime
    # 组织名：GET /auth/me 与 PATCH /auth/me 两口都回填（一次 join，R27 让两口同形状），
    # register/login 的响应里它是 None —— from_attributes 的模型没有这个属性时走默认值，不炸（R6）。
    organization_name: str | None = None


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str


class AuthOut(TokenPairOut):
    user: UserOut


class LogoutOut(BaseModel):
    ok: bool


class AuditLogItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    organization_id: uuid.UUID | None
    user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict | None
    request_id: str | None
    created_at: datetime


class AuditLogPage(BaseModel):
    """06 §7.1 分页信封（新端点直接按契约实现，limit/offset 旧端点不回填）。"""
    items: list[AuditLogItemOut]
    total: int
    page: int
    page_size: int
