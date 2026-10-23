import asyncio
import json
import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

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
# function 是舊版 API 的 tool 結果角色，內容同樣來自外部
SCREENED_ROLES = ("tool", "function")
# 與 spotlight() 的名稱規則相同，稽核與邊界上的名稱才會一致
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
Message = dict[str, Any]
T = TypeVar("T")


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
        max_total_chars: int = 1_000_000,
        clock: Clock = utc_now,
    ) -> None:
        self._policy = policy
        self._store = store
        self._detector = detector
        self._rules = rules
        self._max_chars = max_chars
        self._max_total_chars = max_total_chars
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
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise GuardrailError(400, "invalid_messages")
        self._check_size(messages)
        messages = deepcopy(messages)
        labels = _tool_labels(messages)
        findings = await self._offload(self._screen_tools, messages, labels, config)
        injected = sorted({f.rule for f in findings if f.rule != HIDDEN_RULE})
        audit = _Audit(agent_id, key_alias, model, call_id, findings, config, self._rules.version)
        if injected and config.injection.action == "deny":
            await self._write(audit.event("deny", PseudonymMap()))
            raise GuardrailError(400, "prompt_injection_detected", rules=injected)
        pseudonyms = await self._offload(self._pseudonymize, messages, labels, config)
        # 假名化之後才加：固定文字不被遮蔽，插入新訊息也不會讓 labels 的位置錯開
        if config.injection.system_notice:
            _add_notice(messages)
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

    async def _offload(self, work: Callable[..., T], *args: Any) -> T:
        # 規則比對與 PII 偵測是 CPU 工作，放到 thread 才不會卡住 gateway 的 event loop
        try:
            return await asyncio.to_thread(work, *args)
        except GuardrailError:
            raise
        except Exception as error:
            raise GuardrailError(503, "guardrail_unavailable") from error

    def _check_size(self, messages: list[Message]) -> None:
        texts = _texts(messages)
        too_long = any(len(text) > self._max_chars for text in texts)
        if too_long or sum(map(len, texts)) > self._max_total_chars:
            raise GuardrailError(400, "content_too_large")

    def _screen_tools(
        self, messages: list[Message], labels: dict[int, str], config: GuardrailConfig
    ) -> list[InjectionFinding]:
        """tool 內容換成正規化（或 redact 後）的文字；spotlighting 留到假名化之後。"""
        findings: list[InjectionFinding] = []
        spare = self._max_total_chars - sum(map(len, _texts(messages)))
        for index, label in labels.items():
            message = messages[index]
            original = _text_of(message.get("content"))
            text, hidden = normalize(original)
            # NFKC 可能把一個字元展開成十幾個；比對前先以展開後的長度檢查兩種上限
            spare -= len(text) - len(original)
            if len(text) > self._max_chars or spare < 0:
                raise GuardrailError(400, "content_too_large")
            rules = self._rules.detect(text, config.high_risk_tools)
            if hidden:
                rules.append(HIDDEN_RULE)
            call_id = message.get("tool_call_id")
            digest = sha256_hex(original)
            findings += [
                InjectionFinding(
                    rule=rule,
                    tool=label,
                    tool_call_id=call_id if isinstance(call_id, str) else None,
                    content_sha256=digest,
                )
                for rule in rules
            ]
            injected = [rule for rule in rules if rule != HIDDEN_RULE]
            if injected and config.injection.action == "redact":
                text = REDACTED.format(rules=", ".join(injected))
            message["content"] = text
        return findings

    def _pseudonymize(
        self, messages: list[Message], labels: dict[int, str], config: GuardrailConfig
    ) -> PseudonymMap:
        pseudonyms = PseudonymMap.reserving(_texts(messages))
        masker = Pseudonymizer(self._detector, list(config.pii.entities), list(config.pii.exclude))
        # 先登錄所有訊息裡的值，關鍵字只出現在後面訊息時，前面照抄的同一個值也能被遮蔽
        for text in _pii_texts(messages):
            masker.register(text, pseudonyms)
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = masker.mask(content, pseudonyms)
            elif isinstance(content, list):
                for part in _text_parts(content):
                    part["text"] = masker.mask(part["text"], pseudonyms)
            for function in _functions(message):
                function["arguments"] = masker.mask_json(function["arguments"], pseudonyms)
        for index, label in labels.items():
            messages[index]["content"] = spotlight(label, messages[index]["content"])
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


def _text_parts(content: list[Any]) -> list[dict[str, Any]]:
    return [
        part for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part["text"] for part in _text_parts(content))
    return ""


def _calls(message: Message) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _functions(message: Message) -> list[dict[str, Any]]:
    functions = [call.get("function") for call in _calls(message)]
    return [f for f in functions if isinstance(f, dict) and isinstance(f.get("arguments"), str)]


def _texts(messages: list[Message]) -> list[str]:
    texts = [_text_of(m.get("content")) for m in messages]
    texts += [f["arguments"] for m in messages for f in _functions(m)]
    return texts


def _pii_texts(messages: list[Message]) -> Iterator[str]:
    """依訊息順序列出會被遮蔽的文字；tool 參數取 JSON 內的字串值，與 mask_json 一致。"""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            yield from (part["text"] for part in _text_parts(content))
        for function in _functions(message):
            try:
                yield from _json_strings(json.loads(function["arguments"]))
            except ValueError:
                yield function["arguments"]


def _json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)


def _tool_labels(messages: list[Message]) -> dict[int, str]:
    """tool 結果訊息的位置 → 清理後的 tool 名稱；對不到 tool call 時用訊息的 name。"""
    names: dict[str, str] = {}
    for message in messages:
        for call in _calls(message):
            call_id, function = call.get("id"), call.get("function")
            if isinstance(call_id, str) and call_id and isinstance(function, dict):
                names[call_id] = str(function.get("name") or "")
    labels: dict[int, str] = {}
    for index, message in enumerate(messages):
        if message.get("role") in SCREENED_ROLES:
            call_id = message.get("tool_call_id")
            name = names.get(call_id) if isinstance(call_id, str) else None
            raw = name or str(message.get("name") or "")
            labels[index] = _UNSAFE_NAME.sub("", raw)[:64] or "unknown"
    return labels


def _add_notice(messages: list[Message]) -> None:
    for message in messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += SYSTEM_NOTICE
            return
    messages.insert(0, {"role": "system", "content": SYSTEM_NOTICE.strip()})
