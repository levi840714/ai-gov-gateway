from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_gov_gateway.engine.tokens import TokenVerifier

ISSUER = "http://localhost:8080/realms/governance"
AUDIENCE = "ai-gateway"
_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(
    username: str,
    roles: list[str],
    *,
    audience: str | list[str] = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: int = 300,
    foreign: bool = False,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": f"{username}-sub",
        "preferred_username": username,
        "realm_access": {"roles": roles},
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    key = _FOREIGN_KEY if foreign else _SIGNING_KEY
    return jwt.encode(payload, key, algorithm="RS256")


def alice_token() -> str:
    return make_token("alice", ["analyst"])


def bob_token() -> str:
    return make_token("bob", ["compliance_supervisor"])


def carol_token() -> str:
    return make_token("carol", ["compliance_supervisor"])


def make_verifier() -> TokenVerifier:
    public_key = _SIGNING_KEY.public_key()
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, key_resolver=lambda token: public_key)
