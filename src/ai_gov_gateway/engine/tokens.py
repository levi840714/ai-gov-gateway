from collections.abc import Callable
from typing import Any

import jwt

from ai_gov_gateway.engine.models import UserIdentity

KeyResolver = Callable[[str], Any]


class TokenError(Exception):
    """token 無法驗證；訊息只含錯誤類型，不含 token。"""


class TokenVerifier:
    def __init__(self, *, issuer: str, audience: str, key_resolver: KeyResolver) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key_resolver = key_resolver

    def verify(self, token: str) -> UserIdentity:
        try:
            claims = jwt.decode(
                token,
                self._key_resolver(token),
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise TokenError(type(error).__name__) from error
        realm_access = claims.get("realm_access") or {}
        return UserIdentity(
            sub=str(claims["sub"]),
            username=str(claims.get("preferred_username") or claims["sub"]),
            roles=[str(role) for role in realm_access.get("roles", [])],
        )


def jwks_resolver(jwks_url: str) -> KeyResolver:
    client = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve
