import json
import stat

import httpx
import pytest

from ai_gov_gateway.cli_auth import (
    DeviceCode,
    LoginError,
    load_token,
    poll_token,
    save_token,
    start_device_flow,
)

ISSUER = "http://localhost:8080/realms/governance"
CODE = DeviceCode(
    device_code="dev", user_code="ABCD-EFGH", verification_uri="http://kc/device", expires_in=600
)


def _http(responses: list[httpx.Response], seen: list[httpx.Request] | None = None) -> httpx.Client:
    queue = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return next(queue)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_device_flow():
    seen: list[httpx.Request] = []
    body = {
        "device_code": "dev",
        "user_code": "ABCD-EFGH",
        "verification_uri": "http://kc/device",
        "verification_uri_complete": "http://kc/device?user_code=ABCD-EFGH",
        "expires_in": 600,
        "interval": 5,
    }
    code = start_device_flow(_http([httpx.Response(200, json=body)], seen), ISSUER, "agw-cli")
    assert code.user_code == "ABCD-EFGH"
    assert seen[0].url.path.endswith("/protocol/openid-connect/auth/device")


def test_poll_waits_until_authorized():
    sleeps: list[float] = []
    http = _http(
        [
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "tok"}),
        ]
    )
    assert poll_token(http, ISSUER, "agw-cli", CODE, sleep=sleeps.append) == "tok"
    assert sleeps == [5, 10]


def test_poll_stops_on_denied():
    http = _http([httpx.Response(400, json={"error": "access_denied"})])
    with pytest.raises(LoginError) as error:
        poll_token(http, ISSUER, "agw-cli", CODE, sleep=lambda s: None)
    assert error.value.code == "access_denied"


def test_token_file_is_private(tmp_path):
    path = tmp_path / "nested" / "token.json"
    save_token(path, "tok")
    assert load_token(path) == "tok"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"access_token": "tok"}


def test_missing_token_file(tmp_path):
    with pytest.raises(LoginError) as error:
        load_token(tmp_path / "none.json")
    assert error.value.code == "not_logged_in"
