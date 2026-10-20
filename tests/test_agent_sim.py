from scripts.agent_sim import parse_result


def test_allowed_result_keeps_text():
    result = parse_result(False, '{"status": "frozen"}')
    assert (result.status, result.body) == ("allowed", {"text": '{"status": "frozen"}'})


def test_governance_message_is_parsed_after_prefix():
    text = 'Error: {"governance": "pending_approval", "approval_id": "apr_1", "reasons": ["x"]}'
    result = parse_result(True, text)
    assert result.status == "pending_approval"
    assert result.body["approval_id"] == "apr_1"


def test_other_errors():
    assert parse_result(True, "Error: upstream failed").status == "error"


def test_denied_payload():
    text = 'Error: {"governance": "denied", "reasons": ["role_required"]}'
    result = parse_result(True, text)
    assert (result.status, result.body["reasons"]) == ("denied", ["role_required"])


def test_malformed_json_after_brace_is_error():
    assert parse_result(True, 'Error: {"governance": "denied",').status == "error"


def test_trailing_text_after_json_still_parses():
    text = 'Error: {"governance": "pending_approval", "approval_id": "apr_2"} (trace id 42)'
    result = parse_result(True, text)
    assert (result.status, result.body["approval_id"]) == ("pending_approval", "apr_2")
