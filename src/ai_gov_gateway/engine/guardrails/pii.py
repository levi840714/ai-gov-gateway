import importlib
import json
import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import accumulate
from typing import Any, Protocol

from ai_gov_gateway.engine.guardrails.models import GuardrailError, PiiSpan

TOKEN = re.compile(r"<([A-Z][A-Z_]*?)_(\d+)>")
# 這些類別的原值不能出現在 tool call 參數中：被注入的模型可藉此把真正的密鑰帶出去
SECRET_ENTITIES = frozenset({"API_KEY"})
_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
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
    value = value.upper()
    return bool(re.fullmatch(r"[A-Z][12]\d{8}", value)) and _checksum_ok(value[0], value[1:])


def tw_arc_valid(value: str) -> bool:
    value = value.upper()
    if re.fullmatch(r"[A-Z][89]\d{8}", value):
        return _checksum_ok(value[0], value[1:])
    if re.fullmatch(r"[A-Z][A-D]\d{8}", value):
        # 舊式居留證的第二碼是字母，取其代碼的個位數計算
        return _checksum_ok(value[0], str(_LETTERS[value[1]] % 10) + value[2:])
    return False


def _fold(text: str) -> str:
    """全形 ASCII 轉半形，長度不變，範圍可直接對回原文。"""
    return text.translate(_FULLWIDTH)


def _ascii_only(text: str) -> str:
    # Presidio 的 \b 與 \w 把中文當成字元，緊鄰中文的 email 與卡號會被漏掉
    return _NON_ASCII.sub(" ", _fold(text))


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


_MOBILE = r"(?:(?:\+886|886)[- ]?|0)9\d{2}[- ]?\d{3}[- ]?\d{3}"
# 市話的區碼後一定要有括號或分隔，沒有分隔的 10 碼數字太容易和其他數字混淆
_LANDLINE = "|".join(
    [
        r"\(0[2-8]\) ?\d{3,4}[- ]?\d{4}",
        r"\(0[2-8]\d\) ?\d{3}[- ]?\d{3,4}",
        r"0[2-8][- ]\d{3,4}[- ]?\d{4}",
        r"0[2-8]\d[- ]\d{3}[- ]?\d{3,4}",
        r"\+886[- ]?[2-8]\d?[- ]?\d{3,4}[- ]?\d{3,4}",
    ]
)

_RECOGNIZERS = {
    "TW_ID": _RegexRecognizer(re.compile(_L + r"([A-Za-z][12]\d{8})" + _R), tw_id_valid),
    "TW_ARC": _RegexRecognizer(re.compile(_L + r"([A-Za-z][89A-Da-d]\d{8})" + _R), tw_arc_valid),
    "PHONE": _RegexRecognizer(re.compile(_L + r"(?<!\d-)(" + _MOBILE + "|" + _LANDLINE + ")" + _R)),
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
            r"|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{35}"
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


class _PresidioScan:
    """直接套用 Presidio recognizer 的 pattern 與驗證。

    不呼叫 analyze()：它的去重是 O(n²)，大量比對時會拖垮 gateway。
    """

    def __init__(self, recognizer: Any, min_score: float) -> None:
        regex = importlib.import_module("regex")

        self._recognizer = recognizer
        self._min_score = min_score
        self._patterns = [
            (p.score, regex.compile(p.regex, recognizer.global_regex_flags))
            for p in recognizer.patterns
        ]

    def spans(self, entity: str, text: str) -> list[PiiSpan]:
        from presidio_analyzer.pattern_recognizer import REGEX_TIMEOUT_SECONDS

        found = []
        for base_score, compiled in self._patterns:
            for match in compiled.finditer(text, timeout=REGEX_TIMEOUT_SECONDS):
                if match.end() == match.start():
                    continue
                if self._recognizer.invalidate_result(match.group()):
                    continue
                valid = self._recognizer.validate_result(match.group())
                score = base_score if valid is None else (1.0 if valid else 0.0)
                if score >= self._min_score:
                    found.append(PiiSpan(entity=entity, start=match.start(), end=match.end()))
        return found


class PresidioDetector:
    """EMAIL 與 CREDIT_CARD 用 Presidio 的 recognizer，台灣格式與 API key 用自訂規則。"""

    MIN_SCORE = 0.5

    def __init__(self) -> None:
        # 只用個別 recognizer、不建立 AnalyzerEngine，因此不需要 spaCy 語言模型
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
        )
        from tldextract import TLDExtract

        class OfflineEmailRecognizer(EmailRecognizer):
            # 預設的全域 extractor 會下載 Public Suffix List 且沒有逾時；改用私有的內建快照
            def __init__(self) -> None:
                super().__init__()
                self._tld = TLDExtract(suffix_list_urls=(), cache_dir=None)

            def validate_result(self, pattern_text: str) -> bool:
                return self._tld(pattern_text).fqdn != ""

        self.email_recognizer = OfflineEmailRecognizer()
        self._presidio = {
            "EMAIL": _PresidioScan(self.email_recognizer, self.MIN_SCORE),
            "CREDIT_CARD": _PresidioScan(CreditCardRecognizer(), self.MIN_SCORE),
        }

    def detect(self, text: str, entities: list[str]) -> list[PiiSpan]:
        folded, ascii_text = _fold(text), _ascii_only(text)
        spans: list[PiiSpan] = []
        for entity in entities:
            if entity in self._presidio:
                spans += self._presidio[entity].spans(entity, ascii_text)
            elif entity in _RECOGNIZERS:
                spans += _RECOGNIZERS[entity].spans(entity, folded)
        return spans


def select_spans(text: str, spans: list[PiiSpan], exclude: list[str]) -> list[PiiSpan]:
    """去掉與排除格式重疊的範圍；彼此重疊時合併為聯集（類型取較長者），依位置排序。"""
    blocked = sorted(m.span() for name in exclude for m in EXCLUSIONS[name].finditer(text))
    starts = [start for start, _ in blocked]
    reach = list(accumulate((end for _, end in blocked), max))

    def is_blocked(span: PiiSpan) -> bool:
        count = bisect_left(starts, span.end)
        return count > 0 and reach[count - 1] > span.start

    merged: list[PiiSpan] = []
    for span in sorted((s for s in spans if not is_blocked(s)), key=lambda s: (s.start, -s.end)):
        if merged and span.start < merged[-1].end:
            last = merged[-1]
            longer = span if span.end - span.start > last.end - last.start else last
            merged[-1] = PiiSpan(
                entity=longer.entity, start=last.start, end=max(last.end, span.end)
            )
        else:
            merged.append(span)
    return merged


_END = ""


def _trie_pattern(values: Iterable[str]) -> re.Pattern[str]:
    """把已登錄原值編成共用前綴的樹狀 regex；逐值的 alternation 會讓每個位置都試過全部的值。"""
    root: dict[str, Any] = {}
    for value in values:
        node = root
        for char in value:
            node = node.setdefault(char, {})
        node[_END] = True

    def build(node: dict[str, Any], last: str) -> str:
        literal = ""
        while len(node) == 1 and _END not in node:
            [(char, node)] = node.items()
            literal += re.escape(char)
            last = char
        # 先試較長的分支、最後才是在此結束，因此同一位置取到最長且符合邊界的值
        branches = [re.escape(c) + build(child, c) for c, child in sorted(node.items()) if c]
        if _END in node:
            branches.append(_R if last.isalnum() else "")
        return literal + (branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")")

    alnum = {c: child for c, child in root.items() if c.isalnum()}
    others = {c: child for c, child in root.items() if not c.isalnum()}
    parts = [re.escape(c) + build(child, c) for c, child in sorted(others.items())]
    if alnum:
        parts.append(_L + build(alnum, ""))
    return re.compile("|".join(parts))


def _known_pattern(values: set[str]) -> re.Pattern[str]:
    try:
        return _trie_pattern(values)
    except (RecursionError, re.error) as error:
        # 逐值比對的備援在最壞情況下要數秒，寧可拒絕請求
        raise GuardrailError(400, "too_many_pii_values") from error


class PseudonymMap:
    """單次請求的代號對照；只存在記憶體，不寫入稽核或日誌。"""

    # 已登錄原值的比對成本是 文字長度 × 值的數量，上限讓最壞情況維持在一秒內
    MAX_VALUES = 1000
    # 涵蓋常見的 JWT；過深的前綴樹在編譯時會失敗並拒絕請求
    MAX_VALUE_CHARS = 2048
    # 前綴樹的編譯時間與記憶體隨所有值的總長度成長；各值互不相同時幾乎沒有共用前綴
    MAX_TOTAL_VALUE_CHARS = 65_536

    def __init__(self, reserved: dict[str, set[int]] | None = None) -> None:
        self._reserved = reserved or {}
        self._tokens: dict[tuple[str, str], str] = {}
        self._values: dict[str, str] = {}
        self._entities: dict[str, str] = {}
        self._next: dict[str, int] = {}
        self._known: tuple[re.Pattern[str], dict[str, str]] | None = None
        self._total_chars = 0

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
            if len(value) > self.MAX_VALUE_CHARS:
                raise GuardrailError(400, "pii_value_too_long")
            too_many = len(self._tokens) >= self.MAX_VALUES
            if too_many or self._total_chars + len(value) > self.MAX_TOTAL_VALUE_CHARS:
                raise GuardrailError(400, "too_many_pii_values")
            self._total_chars += len(value)
            number = self._next.get(entity, 1)
            while number in self._reserved.get(entity, set()):
                number += 1
            token = f"<{entity}_{number}>"
            self._next[entity] = number + 1
            self._tokens[key] = token
            self._values[token] = value
            self._entities[token] = entity
            self._known = None
        return self._tokens[key]

    @property
    def value_chars(self) -> int:
        """已登錄原值的總字元數；暫存對照表的一方以此限制記憶體。"""
        return self._total_chars

    def discard_cache(self) -> None:
        """丟掉編譯好的比對樹，只留代號與原值；之後若再比對會重新建立。"""
        self._known = None

    def occurrences(self, text: str) -> list[PiiSpan]:
        """text 中每一處獨立出現的已登錄原值（較長的優先），不含落在我們自己代號內的部分。"""
        if not self._values:
            return []
        if self._known is None:
            values = {value for _, value in self._tokens}
            entity_of = {value: entity for entity, value in reversed(list(self._tokens))}
            self._known = (_known_pattern(values), entity_of)
        pattern, entity_of = self._known
        own = [m.span() for m in TOKEN.finditer(text) if m.group(0) in self._values]
        starts = [start for start, _ in own]
        ends = [end for _, end in own]
        found = []
        for match in pattern.finditer(text):
            inside = bisect_left(starts, match.end())
            if inside and ends[inside - 1] > match.start():
                continue
            found.append(
                PiiSpan(entity=entity_of[match.group(0)], start=match.start(), end=match.end())
            )
        return found

    def counts(self) -> dict[str, int]:
        return dict(Counter(self._entities.values()))

    def restore(self, text: str, skip_entities: frozenset[str] = frozenset()) -> str:
        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            if self._entities.get(token) in skip_entities:
                return token
            return self._values.get(token, token)

        return TOKEN.sub(replace, text)

    def restore_json(self, text: str, skip_entities: frozenset[str] = SECRET_ENTITIES) -> str:
        try:
            value = json.loads(text)
        except ValueError:
            return self.restore(text, skip_entities)
        changed = False

        def restore_string(item: str) -> str:
            nonlocal changed
            restored = self.restore(item, skip_entities)
            changed = changed or restored != item
            return restored

        restored_value = _map_strings(value, restore_string)
        # 沒有可還原的代號時原樣回傳，避免重新序列化改動數字精度與格式
        return json.dumps(restored_value, ensure_ascii=False) if changed else text


class Pseudonymizer:
    def __init__(self, detector: PiiDetector, entities: list[str], exclude: list[str]) -> None:
        self._detector = detector
        self._entities = entities
        self._exclude = exclude

    def register(self, text: str, pseudonyms: PseudonymMap) -> None:
        """只登錄偵測到的原值、不改寫文字，讓同一值在其他訊息裡也能被遮蔽。"""
        for span in self._spans(text, None):
            pseudonyms.token(span.entity, text[span.start : span.end])

    def mask(self, text: str, pseudonyms: PseudonymMap) -> str:
        spans = self._spans(text, pseudonyms)
        # 先依出現順序取得代號，再依序拼接，位置才不會位移
        pieces: list[str] = []
        position = 0
        for span in spans:
            pieces += [
                text[position : span.start],
                pseudonyms.token(span.entity, text[span.start : span.end]),
            ]
            position = span.end
        pieces.append(text[position:])
        return "".join(pieces)

    def _spans(self, text: str, pseudonyms: PseudonymMap | None) -> list[PiiSpan]:
        # 已登錄的值排在前面，與偵測結果等長時沿用原本的類型與代號
        known = pseudonyms.occurrences(text) if pseudonyms else []
        detected = self._detector.detect(text, self._entities)
        return select_spans(text, known + detected, self._exclude)

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
