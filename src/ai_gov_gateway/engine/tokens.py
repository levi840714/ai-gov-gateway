from collections.abc import Callable, Iterable
from typing import Any

import jwt

from ai_gov_gateway.engine.models import UserIdentity

KeyResolver = Callable[[str], Any]


class TokenError(Exception):
    """token 無法驗證；訊息只含錯誤類型，不含 token。"""


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        allowed_clients: Iterable[str],
        key_resolver: KeyResolver,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._allowed_clients = frozenset(allowed_clients)
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
        # 服務帳號的 client_credentials token 也有相同 audience，不能當成委託的使用者
        if claims.get("azp") not in self._allowed_clients:
            raise TokenError("ClientNotAllowed")
        return UserIdentity(
            sub=str(claims["sub"]),
            username=str(claims.get("preferred_username") or claims["sub"]),
            roles=_roles_of(claims),
        )


def _roles_of(claims: dict[str, Any]) -> list[str]:
    realm_access = claims.get("realm_access", {})
    roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else None
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise TokenError("InvalidRealmAccess")
    return roles


def jwks_resolver(jwks_url: str) -> KeyResolver:
    client = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve
