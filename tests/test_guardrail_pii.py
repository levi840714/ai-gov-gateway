import json
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


def test_email_validation_uses_bundled_suffix_list_only(detector):
    import tldextract

    assert tldextract.tldextract.TLD_EXTRACTOR.suffix_list_urls == ()


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
