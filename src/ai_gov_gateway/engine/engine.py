import asyncio
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ai_gov_gateway.engine.audit import approval_event
from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.fingerprint import call_fingerprint, parse_agent_key
from ai_gov_gateway.engine.models import (
    AgentRef,
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditEventType,
    Decision,
    Outcome,
    PolicyDecision,
    ToolCall,
    UserIdentity,
)
from ai_gov_gateway.engine.policy import PolicyClient, PolicyError
from ai_gov_gateway.engine.store import (
    ApprovalConflict,
    GovernanceStore,
    StoreError,
    StoreUnavailable,
)
from ai_gov_gateway.engine.tokens import TokenError, TokenVerifier

OPEN_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
MAX_ATTEMPTS = 3


class _Denied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def new_approval_id() -> str:
    return "apr_" + secrets.token_hex(8)


def policy_input(agent: AgentRef, user: UserIdentity, call: ToolCall) -> dict[str, Any]:
    return {
        "agent": agent.model_dump(),
        "user": user.model_dump(),
        "tool": {"server": call.server, "name": call.tool},
        "arguments": call.arguments,
    }


class GovernanceEngine:
    def __init__(
        self,
        *,
        verifier: TokenVerifier,
        policy: PolicyClient,
        store: GovernanceStore,
        approval_ttl: timedelta,
        approver_role: str,
        clock: Clock = utc_now,
        new_id: Callable[[], str] = new_approval_id,
    ) -> None:
        self._verifier = verifier
        self._policy = policy
        self._store = store
        self._ttl = approval_ttl
        self._approver_role = approver_role
        self._clock = clock
        self._new_id = new_id

    async def decide(self, call: ToolCall) -> Outcome:
        """一律 fail-closed：無法判斷或無法寫入稽核時拒絕。"""
        agent = parse_agent_key(call.key_alias)
        user: UserIdentity | None = None
        try:
            if agent is None:
                raise _Denied("unregistered_agent_version")
            user = await self._verify_user(call.user_token)
            decision = await self._policy.evaluate(policy_input(agent, user, call))
            if decision.decision is Decision.REQUIRE_APPROVAL:
                return await self._require_approval(agent, user, call, decision)
            outcome = (
                Outcome.allowed(decision.reasons)
                if decision.decision is Decision.ALLOW
                else Outcome.denied(decision.reasons)
            )
            await self._record(call, agent, user, outcome, decision.policy_version)
            return outcome
        except _Denied as denied:
            return await self._deny(call, agent, user, denied.code)
        except PolicyError as error:
            return await self._deny(call, agent, user, error.code)
        except StoreUnavailable:
            return Outcome.denied(["governance_store_unavailable"])
        except StoreError:
            return Outcome.denied(["audit_write_failed"])

    async def _verify_user(self, token: str | None) -> UserIdentity:
        if not token:
            raise _Denied("missing_user_token")
        try:
            return await asyncio.to_thread(self._verifier.verify, token)
        except TokenError as error:
            raise _Denied("invalid_user_token") from error

    async def _deny(
        self, call: ToolCall, agent: AgentRef | None, user: UserIdentity | None, code: str
    ) -> Outcome:
        outcome = Outcome.denied([code])
        try:
            await self._record(call, agent, user, outcome, None)
        except StoreUnavailable:
            return Outcome.denied([code, "governance_store_unavailable"])
        except StoreError:
            return Outcome.denied([code, "audit_write_failed"])
        return outcome

    async def _require_approval(
        self, agent: AgentRef, user: UserIdentity, call: ToolCall, decision: PolicyDecision
    ) -> Outcome:
        fingerprint = call_fingerprint(agent, user.sub, call.server, call.tool, call.arguments)
        # 並發請求可能搶先使用或建立審批，重新查詢後依現況處理；超過次數就拒絕
        for _ in range(MAX_ATTEMPTS):
            existing = await self._current_approval(fingerprint)
            if existing is None:
                outcome = await self._create_pending(agent, user, call, decision, fingerprint)
            elif existing.status is ApprovalStatus.APPROVED:
                outcome = await self._redeem(agent, user, call, decision, existing)
            else:
                outcome = Outcome.pending(existing.id, existing.expires_at, decision.reasons)
                await self._record(call, agent, user, outcome, decision.policy_version, existing.id)
            if outcome is not None:
                return outcome
        raise _Denied("approval_conflict")

    async def _redeem(
        self,
        agent: AgentRef,
        user: UserIdentity,
        call: ToolCall,
        decision: PolicyDecision,
        approval: Approval,
    ) -> Outcome | None:
        outcome = Outcome.allowed(decision.reasons)
        redeemed = self._event(
            "approval_redeemed", call, agent, user, outcome, decision.policy_version, approval.id
        )
        used = await self._store.transition(
            approval.id,
            from_statuses=(ApprovalStatus.APPROVED,),
            to=ApprovalStatus.USED,
            at=self._clock(),
            audit=redeemed,
        )
        return outcome if used else None

    async def _create_pending(
        self,
        agent: AgentRef,
        user: UserIdentity,
        call: ToolCall,
        decision: PolicyDecision,
        fingerprint: str,
    ) -> Outcome | None:
        now = self._clock()
        approval = Approval(
            id=self._new_id(),
            fingerprint=fingerprint,
            status=ApprovalStatus.PENDING,
            agent=agent,
            user_sub=user.sub,
            username=user.username,
            server=call.server,
            tool=call.tool,
            arguments=call.arguments,
            reasons=decision.reasons,
            approver_role=decision.approver_role or self._approver_role,
            created_at=now,
            expires_at=now + self._ttl,
        )
        outcome = Outcome.pending(approval.id, approval.expires_at, decision.reasons)
        requested = self._event(
            "approval_requested", call, agent, user, outcome, decision.policy_version, approval.id
        )
        try:
            await self._store.create_approval(approval, requested)
        except ApprovalConflict:
            return None
        return outcome

    async def _current_approval(self, fingerprint: str) -> Approval | None:
        approval = await self._store.open_approval(fingerprint)
        now = self._clock()
        if approval is None or approval.expires_at > now:
            return approval
        await self._store.transition(
            approval.id,
            from_statuses=OPEN_STATUSES,
            to=ApprovalStatus.EXPIRED,
            at=now,
            audit=approval_event("approval_expired", approval, decision=ApprovalStatus.EXPIRED),
        )
        return None

    async def _record(
        self,
        call: ToolCall,
        agent: AgentRef | None,
        user: UserIdentity | None,
        outcome: Outcome,
        policy_version: str | None,
        approval_id: str | None = None,
    ) -> None:
        event = self._event("decision", call, agent, user, outcome, policy_version, approval_id)
        await self._store.append_audit(event, self._clock())

    @staticmethod
    def _event(
        event_type: AuditEventType,
        call: ToolCall,
        agent: AgentRef | None,
        user: UserIdentity | None,
        outcome: Outcome,
        policy_version: str | None,
        approval_id: str | None,
    ) -> AuditEvent:
        return AuditEvent(
            event_type=event_type,
            agent_id=agent.id if agent else None,
            agent_version=agent.version if agent else None,
            key_alias=call.key_alias,
            user_sub=user.sub if user else None,
            username=user.username if user else None,
            server=call.server,
            tool=call.tool,
            arguments=call.arguments,
            decision=outcome.status,
            reasons=outcome.reasons,
            approval_id=approval_id,
            policy_version=policy_version,
        )
