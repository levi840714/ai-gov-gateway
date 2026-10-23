from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from ai_gov_gateway.engine.clock import Clock, utc_now
from ai_gov_gateway.engine.fingerprint import sha256_hex
from ai_gov_gateway.engine.guardrails.injection import (
    REDACTED,
    SYSTEM_NOTICE,
    RuleDetector,
    normalize,
    spotlight,
)
from ai_gov_gateway.engine.guardrails.models import (
    GuardrailConfig,
    GuardrailError,
    InjectionFinding,
)
from ai_gov_gateway.engine.guardrails.pii import PiiDetector, Pseudonymizer, PseudonymMap
from ai_gov_gateway.engine.models import AuditEvent
from ai_gov_gateway.engine.policy import PolicyError
from ai_gov_gateway.engine.store import GovernanceStore, StoreError, StoreUnavailable

HIDDEN_RULE = "hidden.chars"
Message = dict[str, Any]


class GuardrailPolicy(Protocol):
    async def llm_config(self, agent_id: str) -> GuardrailConfig: ...


def agent_of(key_alias: str | None) -> str:
    """LLM key 與 MCP key 都以 @ 之前的部分作為 agent，例如 kyt-agent 與 kyt-agent@1.5。"""
    return key_alias.split("@", 1)[0] if key_alias else ""


@dataclass
class GuardedRequest:
    messages: list[Message]
    pseudonyms: PseudonymMap


class LlmGuardrail:
    def __init__(
        self,
        *,
        policy: GuardrailPolicy,
        store: GovernanceStore,
        detector: PiiDetector,
        rules: RuleDetector,
        max_chars: int,
        clock: Clock = utc_now,
    ) -> None:
        self._policy = policy
        self._store = store
        self._detector = detector
        self._rules = rules
        self._max_chars = max_chars
        self._clock = clock

    async def guard_request(
        self,
        *,
        key_alias: str | None,
        model: str | None,
        call_id: str | None,
        messages: list[Message],
    ) -> GuardedRequest:
        """一律 fail-closed：設定、偵測或稽核任一步失敗都拒絕請求。"""
        agent_id = agent_of(key_alias)
        config = await self._config(agent_id)
        if config.agent_status == "suspended":
            raise GuardrailError(403, "agent_suspended")
        messages = deepcopy(messages)
        if any(len(text) > self._max_chars for text in _texts(messages)):
            raise GuardrailError(400, "content_too_large")
        try:
            findings = self._screen_tools(messages, config)
        except Exception as error:
            raise GuardrailError(503, "guardrail_unavailable") from error
        injected = sorted({f.rule for f in findings if f.rule != HIDDEN_RULE})
        audit = _Audit(agent_id, key_alias, model, call_id, findings, config, self._rules.version)
        if injected and config.injection.action == "deny":
            await self._write(audit.event("deny", PseudonymMap()))
            raise GuardrailError(400, "prompt_injection_detected", rules=injected)
        if config.injection.system_notice:
            _add_notice(messages)
        try:
            pseudonyms = self._pseudonymize(messages, config)
        except Exception as error:
            raise GuardrailError(503, "guardrail_unavailable") from error
        if findings or pseudonyms:
            decision = config.injection.action if injected else ("flag" if findings else "pass")
            await self._write(audit.event(decision, pseudonyms))
        return GuardedRequest(messages=messages, pseudonyms=pseudonyms)

    async def _config(self, agent_id: str) -> GuardrailConfig:
        try:
            return await self._policy.llm_config(agent_id)
        except PolicyError as error:
            code = "policy_unavailable" if error.code == "policy_unavailable" else "policy_error"
            raise GuardrailError(503, code) from error

    def _screen_tools(
        self, messages: list[Message], config: GuardrailConfig
    ) -> list[InjectionFinding]:
        names = _tool_names(messages)
        findings: list[InjectionFinding] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            call_id = message.get("tool_call_id")
            name = names.get(str(call_id), str(message.get("name") or ""))
            original = _text_of(message.get("content"))
            text, hidden = normalize(original)
            rules = self._rules.detect(text, config.high_risk_tools)
            if hidden:
                rules.append(HIDDEN_RULE)
            digest = sha256_hex(original)
            findings += [
                InjectionFinding(rule=rule, tool=name, tool_call_id=call_id, content_sha256=digest)
                for rule in rules
            ]
            injected = [rule for rule in rules if rule != HIDDEN_RULE]
            if injected and config.injection.action == "redact":
                text = REDACTED.format(rules=", ".join(injected))
            message["content"] = spotlight(name, text)
        return findings

    def _pseudonymize(self, messages: list[Message], config: GuardrailConfig) -> PseudonymMap:
        pseudonyms = PseudonymMap.reserving(_texts(messages))
        masker = Pseudonymizer(self._detector, list(config.pii.entities), list(config.pii.exclude))
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = masker.mask(content, pseudonyms)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = masker.mask(part["text"], pseudonyms)
            for function in _functions(message):
                function["arguments"] = masker.mask_json(function["arguments"], pseudonyms)
        return pseudonyms

    async def _write(self, event: AuditEvent) -> None:
        try:
            await self._store.append_audit(event, self._clock())
        except (StoreUnavailable, StoreError) as error:
            raise GuardrailError(503, "audit_write_failed") from error


@dataclass(frozen=True)
class _Audit:
    agent_id: str
    key_alias: str | None
    model: str | None
    call_id: str | None
    findings: list[InjectionFinding]
    config: GuardrailConfig
    rules_version: str

    def event(self, decision: str, pseudonyms: PseudonymMap) -> AuditEvent:
        return AuditEvent(
            event_type="llm_guardrail",
            agent_id=self.agent_id or None,
            key_alias=self.key_alias,
            decision=decision,
            reasons=sorted({f.rule for f in self.findings}),
            policy_version=self.config.policy_version,
            details={
                "model": self.model,
                "call_id": self.call_id,
                "pii": pseudonyms.counts(),
                "injection": [{**f.model_dump(), "action": self._action(f)} for f in self.findings],
                "rules_version": self.rules_version,
            },
        )

    def _action(self, finding: InjectionFinding) -> str:
        # 不可見字元只移除、不攔截，因此一律記為 flag
        return "flag" if finding.rule == HIDDEN_RULE else self.config.injection.action


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _functions(message: Message) -> list[dict[str, Any]]:
    functions: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and isinstance(function.get("arguments"), str):
            functions.append(function)
    return functions


def _texts(messages: list[Message]) -> list[str]:
    texts = [_text_of(m.get("content")) for m in messages]
    texts += [f["arguments"] for m in messages for f in _functions(m)]
    return texts


def _tool_names(messages: list[Message]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                names[str(call.get("id"))] = str(call["function"].get("name") or "")
    return names


def _add_notice(messages: list[Message]) -> None:
    for message in messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += SYSTEM_NOTICE
            return
    messages.insert(0, {"role": "system", "content": SYSTEM_NOTICE.strip()})
