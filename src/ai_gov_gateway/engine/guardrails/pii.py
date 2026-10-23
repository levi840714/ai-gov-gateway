import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from ai_gov_gateway.engine.guardrails.models import PiiSpan

TOKEN = re.compile(r"<([A-Z][A-Z_]*?)_(\d+)>")
# 不能夾在英數字或小數中間：避免在 hex 地址、交易 hash、長數字與金額裡誤抓
_L = r"(?<![A-Za-z0-9.])"
_R = r"(?![A-Za-z0-9]|\.\d)"

# 身分證與居留證的字母代碼（I、O 等不依字母順序）
_LETTERS = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36), strict=True))
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 1)


def _checksum_ok(letter: str, digits: str) -> bool:
    code = _LETTERS[letter]
    body = sum(int(d) * w for d, w in zip(digits[:8], _WEIGHTS, strict=True))
    return (code // 10 + code % 10 * 9 + body + int(digits[8])) % 10 == 0


def tw_id_valid(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][12]\d{8}", value)) and _checksum_ok(value[0], value[1:])


def tw_arc_valid(value: str) -> bool:
    if re.fullmatch(r"[A-Z][89]\d{8}", value):
        return _checksum_ok(value[0], value[1:])
    if re.fullmatch(r"[A-Z][A-D]\d{8}", value):
        # 舊式居留證的第二碼是字母，取其代碼的個位數計算
        return _checksum_ok(value[0], str(_LETTERS[value[1]] % 10) + value[2:])
    return False


def _digit_count(low: int, high: int) -> Callable[[str], bool]:
    return lambda value: low <= sum(c.isdigit() for c in value) <= high


@dataclass(frozen=True)
class _RegexRecognizer:
    pattern: re.Pattern[str]
    validate: Callable[[str], bool] | None = None
    group: int = 1

    def spans(self, entity: str, text: str) -> list[PiiSpan]:
        found = []
        for match in self.pattern.finditer(text):
            value = match.group(self.group)
            if self.validate is None or self.validate(value):
                found.append(
                    PiiSpan(entity=entity, start=match.start(self.group), end=match.end(self.group))
                )
        return found


_RECOGNIZERS = {
    "TW_ID": _RegexRecognizer(re.compile(_L + r"([A-Z][12]\d{8})" + _R), tw_id_valid),
    "TW_ARC": _RegexRecognizer(re.compile(_L + r"([A-Z][89A-D]\d{8})" + _R), tw_arc_valid),
    "PHONE": _RegexRecognizer(
        re.compile(
            _L
            + r"((?:\+886[- ]?|0)9\d{2}[- ]?\d{3}[- ]?\d{3}"
            + r"|\(0[2-8]\)\d{3,4}-?\d{4}|0[2-8]-\d{3,4}-?\d{4})"
            + _R
        )
    ),
    "BANK_ACCOUNT": _RegexRecognizer(
        re.compile(
            r"(?i)(?:帳號|帳戶|戶名|account(?:\s*(?:no\.?|number))?|acct)[^\d\n]{0,12}"
            + _L
            + r"(\d(?:[- ]?\d){9,17})"
            + _R
        ),
        _digit_count(10, 16),
    ),
    "API_KEY": _RegexRecognizer(
        re.compile(
            r"(?<![A-Za-z0-9_-])(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}"
            r"|xox[bp]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
        )
    ),
}

EXCLUSIONS = {
    "eth_address": re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])"),
    "eth_tx_hash": re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])"),
}


class PiiDetector(Protocol):
    def detect(self, text: str, entities: list[str]) -> list[PiiSpan]: ...


class PresidioDetector:
    """EMAIL 與 CREDIT_CARD 用 Presidio 的 recognizer，台灣格式與 API key 用自訂規則。"""

    MIN_SCORE = 0.5

    def __init__(self) -> None:
        # 只用個別 recognizer、不建立 AnalyzerEngine，因此不需要 spaCy 語言模型
        import tldextract
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
        )

        # email 驗證預設會在第一次使用時下載 Public Suffix List 且沒有逾時；只用套件內建的快照
        tldextract.tldextract.TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())

        self._presidio: dict[str, tuple[Any, str]] = {
            "EMAIL": (EmailRecognizer(), "EMAIL_ADDRESS"),
            "CREDIT_CARD": (CreditCardRecognizer(), "CREDIT_CARD"),
        }

    def detect(self, text: str, entities: list[str]) -> list[PiiSpan]:
        spans: list[PiiSpan] = []
        for entity in entities:
            if entity in self._presidio:
                recognizer, name = self._presidio[entity]
                spans += [
                    PiiSpan(entity=entity, start=r.start, end=r.end)
                    for r in recognizer.analyze(text, [name], None)
                    if r.score >= self.MIN_SCORE
                ]
            elif entity in _RECOGNIZERS:
                spans += _RECOGNIZERS[entity].spans(entity, text)
        return spans


def select_spans(text: str, spans: list[PiiSpan], exclude: list[str]) -> list[PiiSpan]:
    """去掉與排除格式重疊的範圍；彼此重疊時保留較長的，回傳依位置排序。"""
    blocked = [m.span() for name in exclude for m in EXCLUSIONS[name].finditer(text)]
    kept: list[PiiSpan] = []
    for span in sorted(spans, key=lambda s: (s.start, s.start - s.end)):
        if any(span.start < end and start < span.end for start, end in blocked):
            continue
        if kept and span.start < kept[-1].end:
            if span.end - span.start > kept[-1].end - kept[-1].start:
                kept[-1] = span
            continue
        kept.append(span)
    return kept


class PseudonymMap:
    """單次請求的代號對照；只存在記憶體，不寫入稽核或日誌。"""

    def __init__(self, reserved: dict[str, set[int]] | None = None) -> None:
        self._reserved = reserved or {}
        self._tokens: dict[tuple[str, str], str] = {}
        self._values: dict[str, str] = {}
        self._entities: dict[str, str] = {}
        self._next: dict[str, int] = {}

    @classmethod
    def reserving(cls, texts: Iterable[str]) -> "PseudonymMap":
        reserved: dict[str, set[int]] = {}
        for text in texts:
            for match in TOKEN.finditer(text):
                reserved.setdefault(match.group(1), set()).add(int(match.group(2)))
        return cls(reserved)

    def __bool__(self) -> bool:
        return bool(self._values)

    def token(self, entity: str, value: str) -> str:
        key = (entity, value)
        if key not in self._tokens:
            number = self._next.get(entity, 1)
            while number in self._reserved.get(entity, set()):
                number += 1
            token = f"<{entity}_{number}>"
            self._next[entity] = number + 1
            self._tokens[key] = token
            self._values[token] = value
            self._entities[token] = entity
        return self._tokens[key]

    def counts(self) -> dict[str, int]:
        return dict(Counter(self._entities.values()))

    def restore(self, text: str) -> str:
        return TOKEN.sub(lambda m: self._values.get(m.group(0), m.group(0)), text)

    def restore_json(self, text: str) -> str:
        try:
            value = json.loads(text)
        except ValueError:
            return self.restore(text)
        return json.dumps(_map_strings(value, self.restore), ensure_ascii=False)


class Pseudonymizer:
    def __init__(self, detector: PiiDetector, entities: list[str], exclude: list[str]) -> None:
        self._detector = detector
        self._entities = entities
        self._exclude = exclude

    def mask(self, text: str, pseudonyms: PseudonymMap) -> str:
        spans = select_spans(text, self._detector.detect(text, self._entities), self._exclude)
        # 先依出現順序取得代號，再由後往前替換，位置才不會位移
        tokens = [pseudonyms.token(s.entity, text[s.start : s.end]) for s in spans]
        for span, token in reversed(list(zip(spans, tokens, strict=True))):
            text = text[: span.start] + token + text[span.end :]
        return text

    def mask_json(self, text: str, pseudonyms: PseudonymMap) -> str:
        try:
            value = json.loads(text)
        except ValueError:
            return self.mask(text, pseudonyms)
        masked = _map_strings(value, lambda s: self.mask(s, pseudonyms))
        return json.dumps(masked, ensure_ascii=False)


def _map_strings(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_map_strings(item, transform) for item in value]
    if isinstance(value, dict):
        return {key: _map_strings(item, transform) for key, item in value.items()}
    return value


class StreamTextRestorer:
    """streaming 時暫存可能是代號開頭的片段，確認完整或不是代號後才送出。"""

    MAX_TOKEN = 32

    def __init__(self, pseudonyms: PseudonymMap) -> None:
        self._pseudonyms = pseudonyms
        self._buffer = ""

    def feed(self, text: str) -> str:
        self._buffer += text
        start = self._buffer.rfind("<")
        pending = start != -1 and ">" not in self._buffer[start:]
        if pending and len(self._buffer) - start <= self.MAX_TOKEN:
            out, self._buffer = self._buffer[:start], self._buffer[start:]
        else:
            out, self._buffer = self._buffer, ""
        return self._pseudonyms.restore(out)

    def flush(self) -> str:
        out, self._buffer = self._buffer, ""
        return self._pseudonyms.restore(out)
