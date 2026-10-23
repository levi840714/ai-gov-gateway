import json
import time
from pathlib import Path

import pytest

from ai_gov_gateway.engine.guardrails.models import PiiSpan
from ai_gov_gateway.engine.guardrails.pii import (
    PresidioDetector,
    Pseudonymizer,
    PseudonymMap,
    StreamTextRestorer,
    select_spans,
    tw_arc_valid,
    tw_id_valid,
)

ALL = ["TW_ID", "TW_ARC", "PHONE", "EMAIL", "CREDIT_CARD", "BANK_ACCOUNT", "API_KEY"]
ADDRESS = "0x1111111111111111111111111111111111111111"
TX = "0x" + "ab12" * 16


@pytest.fixture(scope="module")
def detector() -> PresidioDetector:
    return PresidioDetector()


def _found(detector, text, entities=ALL):
    return [(s.entity, text[s.start : s.end]) for s in detector.detect(text, entities)]


def test_tw_id_checksum():
    assert tw_id_valid("A123456789")
    assert not tw_id_valid("A123456788")
    assert not tw_id_valid("A323456789")


def test_tw_arc_new_and_old_formats():
    assert tw_arc_valid("A800000014")
    assert tw_arc_valid("AA00000009")
    assert not tw_arc_valid("A800000015")
    assert not tw_arc_valid("AE00000009")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("身分證A123456789。", [("TW_ID", "A123456789")]),
        ("居留證 A800000014", [("TW_ARC", "A800000014")]),
        (
            "手機 0912-345-678 或 +886 912345678",
            [("PHONE", "0912-345-678"), ("PHONE", "+886 912345678")],
        ),
        ("市話 (02)2345-6789", [("PHONE", "(02)2345-6789")]),
        ("聯絡 alice@example.com", [("EMAIL", "alice@example.com")]),
        ("卡號 4111 1111 1111 1111", [("CREDIT_CARD", "4111 1111 1111 1111")]),
        ("匯款帳號：012-345678-9012", [("BANK_ACCOUNT", "012-345678-9012")]),
        ("key sk-abcdefghijklmnopqrstuv", [("API_KEY", "sk-abcdefghijklmnopqrstuv")]),
        ("AWS AKIAABCDEFGHIJKLMNOP", [("API_KEY", "AKIAABCDEFGHIJKLMNOP")]),
    ],
)
def test_detects_supported_formats(detector, text, expected):
    assert _found(detector, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "身分證 A123456788",
        "金額 0.0912345678 ETH",
        "區塊 21234567，共 10 筆",
        "1234567890123 個 token",
        f"地址 {ADDRESS}",
        f"交易 {TX}",
    ],
)
def test_ignores_non_pii(detector, text):
    assert select_spans(text, detector.detect(text, ALL), ["eth_address", "eth_tx_hash"]) == []


def test_kyt_tool_outputs_have_no_pii(detector):
    lines = Path("tests/data/kyt_tool_outputs.jsonl").read_text(encoding="utf-8").splitlines()
    exclude = ["eth_address", "eth_tx_hash"]
    hits = {
        text: found
        for text in map(json.loads, lines)
        if (found := select_spans(text, detector.detect(text, ALL), exclude))
    }
    assert hits == {}


def test_only_requested_entities_are_detected(detector):
    assert _found(detector, "alice@example.com A123456789", ["TW_ID"]) == [("TW_ID", "A123456789")]


def test_exclusions_drop_overlapping_spans():
    text = f"{ADDRESS} 0912345678"
    spans = [PiiSpan(entity="PHONE", start=5, end=15), PiiSpan(entity="PHONE", start=43, end=53)]
    assert select_spans(text, spans, ["eth_address"]) == [spans[1]]


def test_overlapping_spans_keep_the_longer_one():
    spans = [
        PiiSpan(entity="PHONE", start=0, end=10),
        PiiSpan(entity="BANK_ACCOUNT", start=0, end=14),
    ]
    assert select_spans("x" * 20, spans, []) == [spans[1]]


def test_same_value_gets_same_token_in_order(detector):
    pseudonyms = PseudonymMap()
    masker = Pseudonymizer(detector, ALL, [])
    first = masker.mask("寄給 a@example.com 與 b@example.com", pseudonyms)
    second = masker.mask("再寄一次 a@example.com", pseudonyms)
    assert first == "寄給 <EMAIL_1> 與 <EMAIL_2>"
    assert second == "再寄一次 <EMAIL_1>"
    assert pseudonyms.counts() == {"EMAIL": 2}


def test_reserved_numbers_are_skipped_and_never_restored(detector):
    pseudonyms = PseudonymMap.reserving(["原文就有 <EMAIL_1>"])
    masked = Pseudonymizer(detector, ALL, []).mask("a@example.com", pseudonyms)
    assert masked == "<EMAIL_2>"
    assert pseudonyms.restore("<EMAIL_1> <EMAIL_2>") == "<EMAIL_1> a@example.com"


def test_mask_json_keeps_valid_json(detector):
    pseudonyms = PseudonymMap()
    masked = Pseudonymizer(detector, ALL, []).mask_json(
        '{"to": ["a@example.com"], "n": 1}', pseudonyms
    )
    assert json.loads(masked) == {"to": ["<EMAIL_1>"], "n": 1}


def test_restore_json_escapes_quotes():
    pseudonyms = PseudonymMap()
    token = pseudonyms.token("EMAIL", 'x"y@example.com')
    restored = pseudonyms.restore_json(json.dumps({"to": token}))
    assert json.loads(restored) == {"to": 'x"y@example.com'}
    assert pseudonyms.restore_json(f"not json {token}") == 'not json x"y@example.com'


def test_email_validation_uses_private_bundled_suffix_list():
    import tldextract

    detector = PresidioDetector()
    assert detector.email_recognizer._tld.suffix_list_urls == ()
    assert tldextract.tldextract.TLD_EXTRACTOR.suffix_list_urls != ()
    assert detector.email_recognizer.validate_result("a@example.com")
    assert not detector.email_recognizer.validate_result("a@localhost")


def test_empty_map_is_falsy():
    assert not PseudonymMap()


def test_stream_restorer_handles_split_tokens():
    pseudonyms = PseudonymMap()
    pseudonyms.token("TW_ID", "A123456789")
    restorer = StreamTextRestorer(pseudonyms)
    out = [
        restorer.feed("abc <T"),
        restorer.feed("W_ID_1> d"),
        restorer.feed("ef <"),
        restorer.flush(),
    ]
    assert "".join(out) == "abc A123456789 def <"
    assert out[0] == "abc "


def test_stream_restorer_releases_long_non_token_text():
    restorer = StreamTextRestorer(PseudonymMap())
    assert restorer.feed("<" + "x" * 40) == "<" + "x" * 40


HISTORY = ["我的帳號 0123456789012 請查餘額", "已查詢 0123456789012 的餘額"]


def test_value_echoed_without_context_is_masked_after_sequential_mask(detector):
    pseudonyms = PseudonymMap()
    masker = Pseudonymizer(detector, ALL, [])
    assert [masker.mask(t, pseudonyms) for t in HISTORY] == [
        "我的帳號 <BANK_ACCOUNT_1> 請查餘額",
        "已查詢 <BANK_ACCOUNT_1> 的餘額",
    ]


@pytest.mark.parametrize("history", [HISTORY, HISTORY[::-1]])
def test_register_all_then_mask_covers_either_order(detector, history):
    pseudonyms = PseudonymMap()
    masker = Pseudonymizer(detector, ALL, [])
    for text in history:
        masker.register(text, pseudonyms)
    masked = [masker.mask(t, pseudonyms) for t in history]
    assert all("0123456789012" not in t and "<BANK_ACCOUNT_1>" in t for t in masked)
    assert pseudonyms.counts() == {"BANK_ACCOUNT": 1}


def test_register_does_not_rewrite_text_and_known_values_skip_own_tokens(detector):
    pseudonyms = PseudonymMap()
    masker = Pseudonymizer(detector, ALL, [])
    masker.register("a@example.com", pseudonyms)
    assert masker.mask("<EMAIL_1> 與 a@example.com", pseudonyms) == "<EMAIL_1> 與 <EMAIL_1>"
    assert pseudonyms.occurrences("<EMAIL_1>") == []


def test_known_values_respect_exclusions(detector):
    pseudonyms = PseudonymMap()
    pseudonyms.token("PHONE", "1111111111")
    masked = Pseudonymizer(detector, ALL, ["eth_address"]).mask(f"{ADDRESS} 1111111111", pseudonyms)
    assert masked == f"{ADDRESS} <PHONE_1>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("請寄到alice@example.com謝謝", [("EMAIL", "alice@example.com")]),
        ("卡號4111111111111111，到期", [("CREDIT_CARD", "4111111111111111")]),
        ("好的，已寄信到alice@example.com。", [("EMAIL", "alice@example.com")]),
        ("ａｌｉｃｅ＠example.com", [("EMAIL", "ａｌｉｃｅ＠example.com")]),
        ("Ａ１２３４５６７８９", [("TW_ID", "Ａ１２３４５６７８９")]),
        ("身分證a123456789與居留證a800000014", [("TW_ID", "a123456789"), ("TW_ARC", "a800000014")]),
    ],
)
def test_cjk_adjacency_and_fullwidth(detector, text, expected):
    assert _found(detector, text) == expected


def test_mask_only_touches_the_pii_next_to_chinese(detector):
    masked = Pseudonymizer(detector, ALL, []).mask(
        "好的，已寄信到alice@example.com。", PseudonymMap()
    )
    assert masked == "好的，已寄信到<EMAIL_1>。"


@pytest.mark.parametrize(
    "phone",
    [
        "(02) 2345-6789",
        "(02)2345 6789",
        "02 2345 6789",
        "02-2345-6789",
        "+886-2-2345-6789",
        "+886 2 2345 6789",
        "037-123456",
        "049-2345678",
        "(049)2345678",
        "886912345678",
        "+886-912-345-678",
    ],
)
def test_detects_phone_formats(detector, phone):
    assert _found(detector, f"電話 {phone} 謝謝") == [("PHONE", phone)]


@pytest.mark.parametrize("text", ["0223456789", "日期 2026-03-12345678", "886912345678901"])
def test_ignores_bare_or_embedded_numbers(detector, text):
    assert _found(detector, text) == []


@pytest.mark.parametrize("unit", ["a@example.com ", "4111 1111 1111 1111 "])
def test_detection_cost_stays_linear(detector, unit):
    text = unit * (200_000 // len(unit))
    started = time.perf_counter()
    spans = detector.detect(text, ["EMAIL", "CREDIT_CARD"])
    assert len(spans) == 200_000 // len(unit)
    assert time.perf_counter() - started < 1.5


def test_masking_many_spans_stays_fast(detector):
    text = "a@example.com " * 14_000
    started = time.perf_counter()
    masked = Pseudonymizer(detector, ["EMAIL"], ["eth_address"]).mask(text, PseudonymMap())
    assert masked.startswith("<EMAIL_1> ")
    assert time.perf_counter() - started < 1.5


def test_overlapping_spans_merge_into_their_union():
    spans = [
        PiiSpan(entity="PHONE", start=0, end=10),
        PiiSpan(entity="BANK_ACCOUNT", start=5, end=20),
    ]
    assert select_spans("x" * 30, spans, []) == [PiiSpan(entity="BANK_ACCOUNT", start=0, end=20)]


def test_restore_json_without_issued_tokens_returns_text_unchanged():
    pseudonyms = PseudonymMap()
    pseudonyms.token("EMAIL", "a@example.com")
    raw = '{"amount": 12345678901234567890, "note": "<EMAIL_9>",  "x": 1.0}'
    assert pseudonyms.restore_json(raw) == raw


def test_restore_json_never_restores_api_keys_but_content_restore_does():
    pseudonyms = PseudonymMap()
    key = pseudonyms.token("API_KEY", "sk-abcdefghijklmnopqrstuv")
    email = pseudonyms.token("EMAIL", "a@example.com")
    arguments = json.dumps({"to": email, "auth": key})
    assert json.loads(pseudonyms.restore_json(arguments)) == {"to": "a@example.com", "auth": key}
    assert pseudonyms.restore_json(f"not json {key}") == f"not json {key}"
    assert pseudonyms.restore_json(arguments, frozenset()) != arguments
    assert pseudonyms.restore(key) == "sk-abcdefghijklmnopqrstuv"


@pytest.mark.parametrize(
    "key",
    ["github_pat_" + "A1b2" * 6, "AIza" + "Sy" * 17 + "a", "ghp_" + "a1" * 18],
)
def test_detects_more_api_key_formats(detector, key):
    assert _found(detector, f"key {key}") == [("API_KEY", key)]
