import pytest

from ai_gov_gateway.engine.models import UserIdentity
from ai_gov_gateway.engine.tokens import TokenError
from tests.jwt_helpers import alice_token, make_token, make_verifier


def test_verify_returns_identity():
    assert make_verifier().verify(alice_token()) == UserIdentity(
        sub="alice-sub", username="alice", roles=["analyst"]
    )


def test_verify_accepts_audience_list():
    token = make_token("alice", ["analyst"], audience=["ai-gateway", "account"])
    assert make_verifier().verify(token).username == "alice"


@pytest.mark.parametrize(
    "token",
    [
        make_token("alice", ["analyst"], expires_in=-10),
        make_token("alice", ["analyst"], audience="other"),
        make_token("alice", ["analyst"], issuer="http://evil.test/realms/governance"),
        make_token("alice", ["analyst"], foreign=True),
        "not-a-jwt",
    ],
    ids=["expired", "audience", "issuer", "signature", "garbage"],
)
def test_verify_rejects_invalid_tokens(token):
    with pytest.raises(TokenError) as error:
        make_verifier().verify(token)
    assert token not in str(error.value)
