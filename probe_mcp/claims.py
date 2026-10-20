import base64
import json
from collections.abc import Mapping
from typing import Any

# 只有這些 header 原樣顯示；其餘可能含憑證，一律只顯示長度
VISIBLE_HEADERS = {
    "host",
    "user-agent",
    "accept",
    "content-type",
    "content-length",
    "mcp-protocol-version",
    "mcp-session-id",
    "x-litellm-end-user-id",
    "x-litellm-key-alias",
    "x-litellm-team-id",
    "x-forwarded-for",
}


def decode_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {"error": "not a JWT"}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return {"error": "invalid payload"}
    return claims


def summarize_headers(headers: Mapping[str, str]) -> dict[str, Any]:
    shown: dict[str, str] = {}
    claims: dict[str, Any] | None = None
    for name, value in headers.items():
        key = name.lower()
        if key == "authorization" and value.lower().startswith("bearer "):
            claims = decode_claims(value[7:])
            shown[key] = "<bearer>"
        elif key in VISIBLE_HEADERS:
            shown[key] = value
        else:
            shown[key] = f"<redacted len={len(value)}>"
    return {"headers": shown, "bearer_claims": claims}
