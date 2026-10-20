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
