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
) -> str:
    """依 RFC 8628 輪詢；Keycloak 在逾時後回 expired_token，所以迴圈一定會結束。"""
    interval = code.interval
    while True:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps({"access_token": token}))


def load_token(path: Path) -> str:
    try:
        return str(json.loads(path.read_text())["access_token"])
    except (FileNotFoundError, KeyError, ValueError) as error:
        raise LoginError("not_logged_in") from error
