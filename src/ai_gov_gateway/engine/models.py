import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    USED = "used"


class AgentRef(BaseModel):
    id: str
    version: str
    key_alias: str


class UserIdentity(BaseModel):
    sub: str
    username: str
    roles: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    """hook 從 LiteLLM 取得的一次 tool 呼叫。"""

    key_alias: str | None
    user_token: str | None
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    decision: Decision
    reasons: list[str]
    policy_version: str
    approver_role: str | None = None


OutcomeStatus = Literal["allowed", "denied", "pending_approval"]


class Outcome(BaseModel):
    status: OutcomeStatus
    reasons: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def allowed(cls, reasons: list[str] | None = None) -> "Outcome":
        return cls(status="allowed", reasons=reasons or [])

    @classmethod
    def denied(cls, reasons: list[str]) -> "Outcome":
        return cls(status="denied", reasons=reasons)

    @classmethod
    def pending(cls, approval_id: str, expires_at: datetime, reasons: list[str]) -> "Outcome":
        return cls(
            status="pending_approval",
            reasons=reasons,
            approval_id=approval_id,
            expires_at=expires_at,
        )

    def to_message(self) -> str:
        """hook 阻擋時 raise 的訊息；agent 以 JSON 解析。"""
        body: dict[str, Any] = {"governance": self.status, "reasons": self.reasons}
        if self.approval_id is not None:
            body["approval_id"] = self.approval_id
        if self.expires_at is not None:
            body["expires_at"] = self.expires_at.isoformat()
        return json.dumps(body, ensure_ascii=False)


class Approval(BaseModel):
    id: str
    fingerprint: str
    status: ApprovalStatus
    agent: AgentRef
    user_sub: str
    username: str
    server: str
    tool: str
    arguments: dict[str, Any]
    reasons: list[str]
    approver_role: str
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_by_username: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    used_at: datetime | None = None


AuditEventType = Literal[
    "decision",
    "approval_requested",
    "approval_redeemed",
    "approval_approved",
    "approval_rejected",
    "approval_expired",
]


class AuditEvent(BaseModel):
    event_type: AuditEventType
    agent_id: str | None = None
    agent_version: str | None = None
    key_alias: str | None = None
    user_sub: str | None = None
    username: str | None = None
    actor_sub: str | None = None
    actor_username: str | None = None
    server: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    decision: str | None = None
    reasons: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    policy_version: str | None = None
    comment: str | None = None


class AuditRecord(AuditEvent):
    id: int
    ts: datetime
    prev_hash: str
    hash: str
