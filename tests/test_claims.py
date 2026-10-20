import base64
import json

from probe_mcp.claims import decode_claims, summarize_headers


def _jwt(payload: dict) -> str:
    def part(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(payload)}.sig"


def test_decode_claims_handles_missing_padding():
    assert decode_claims(_jwt({"sub": "alice", "aud": "probe-mcp"})) == {
        "sub": "alice",
        "aud": "probe-mcp",
    }


def test_decode_claims_returns_error_for_non_jwt():
    assert decode_claims("not-a-jwt") == {"error": "not a JWT"}


def test_summarize_headers_decodes_bearer_and_redacts_secrets():
    summary = summarize_headers(
        {
            "Authorization": f"Bearer {_jwt({'sub': 'alice'})}",
            "x-litellm-api-key": "sk-secret",
            "user-agent": "probe-test",
            "x-custom": "abc",
        }
    )
    assert summary["bearer_claims"] == {"sub": "alice"}
    assert summary["headers"]["authorization"] == "<bearer>"
    assert summary["headers"]["x-litellm-api-key"] == "<redacted len=9>"
    assert summary["headers"]["user-agent"] == "probe-test"
    assert summary["headers"]["x-custom"] == "<redacted len=3>"


def test_summarize_headers_without_bearer():
    assert summarize_headers({"host": "probe"}) == {
        "headers": {"host": "probe"},
        "bearer_claims": None,
    }
