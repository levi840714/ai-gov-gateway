import time

import jwt
import pytest

from ai_gov_gateway.engine.models import UserIdentity
from ai_gov_gateway.engine.tokens import TokenError
from tests.jwt_helpers import AUDIENCE, ISSUER, alice_token, make_token, make_verifier


def test_verify_returns_identity():
    assert make_verifier().verify(alice_token()) == UserIdentity(
        sub="alice-sub", username="alice", roles=["analyst"]
    )


def test_verify_accepts_audience_list():
    token = make_token("alice", ["analyst"], audience=["ai-gateway", "account"])
    assert make_verifier().verify(token).username == "alice"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (make_token("alice", ["analyst"], expires_in=-10), "ExpiredSignatureError"),
        (make_token("alice", ["analyst"], audience="other"), "InvalidAudienceError"),
        (
            make_token("alice", ["analyst"], issuer="http://evil.test/realms/governance"),
            "InvalidIssuerError",
        ),
        (make_token("alice", ["analyst"], foreign=True), "InvalidSignatureError"),
        ("not-a-jwt", "DecodeError"),
        (make_token("alice", [], realm_access="admin"), "InvalidRealmAccess"),
        (make_token("alice", [], realm_access={"roles": "admin"}), "InvalidRealmAccess"),
        (make_token("alice", [], realm_access={"roles": [1]}), "InvalidRealmAccess"),
    ],
    ids=[
        "expired",
        "audience",
        "issuer",
        "signature",
        "garbage",
        "realm_access_not_dict",
        "roles_not_list",
        "roles_not_strings",
    ],
)
def test_verify_rejects_invalid_tokens(token, expected):
    with pytest.raises(TokenError) as error:
        make_verifier().verify(token)
    assert str(error.value) == expected
    assert token not in str(error.value)


def test_verify_rejects_unsigned_token():
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice-sub",
        "exp": int(time.time()) + 300,
    }
    token = jwt.encode(payload, None, algorithm="none")
    with pytest.raises(TokenError):
        make_verifier().verify(token)


def test_verify_without_realm_access_yields_no_roles():
    token = make_token("alice", [], realm_access=None)
    assert make_verifier().verify(token).roles == []
