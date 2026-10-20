import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class LoginError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeviceCode(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int
    interval: int = 5


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return f"http_{response.status_code}"


def start_device_flow(http: httpx.Client, issuer: str, client_id: str) -> DeviceCode:
    response = http.post(
        f"{issuer}/protocol/openid-connect/auth/device",
        data={"client_id": client_id, "scope": "openid"},
    )
    if response.status_code != 200:
        raise LoginError(_error_code(response))
    return DeviceCode.model_validate(response.json())


def poll_token(
    http: httpx.Client,
    issuer: str,
    client_id: str,
    code: DeviceCode,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """依 RFC 8628 輪詢；超過 device code 的有效期限即結束，不依賴伺服器回 expired_token。"""
    interval = code.interval
    deadline = now() + code.expires_in
    while True:
        if now() >= deadline:
            raise LoginError("expired_token")
        response = http.post(
            f"{issuer}/protocol/openid-connect/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": code.device_code,
                "client_id": client_id,
            },
        )
        if response.status_code == 200:
            return str(response.json()["access_token"])
        error = _error_code(response)
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise LoginError(error)
        sleep(interval)


def save_token(path: Path, token: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps({"access_token": token}))


def load_token(path: Path) -> str:
    try:
        token = json.loads(path.read_text())["access_token"]
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise LoginError("not_logged_in") from error
    if not isinstance(token, str) or not token:
        raise LoginError("not_logged_in")
    return token
