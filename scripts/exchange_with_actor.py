import json

import httpx

from probe_mcp.claims import decode_claims
from scripts.env import KEYCLOAK_TOKEN_URL, env
from scripts.kc import client_token, user_token

KEYS = ("sub", "aud", "azp", "act", "scope", "preferred_username")

if __name__ == "__main__":
    subject = user_token("alice")
    actor = client_token("kyt-agent", "KYT_AGENT_CLIENT_SECRET")
    print(
        "actor claims:", json.dumps({k: decode_claims(actor).get(k) for k in ("sub", "azp", "aud")})
    )
    response = httpx.post(
        KEYCLOAK_TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "ai-gateway",
            "client_secret": env("AI_GATEWAY_CLIENT_SECRET"),
            "subject_token": subject,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": actor,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "probe-mcp",
        },
    )
    print("status:", response.status_code)
    body = response.json()
    if "access_token" in body:
        claims = decode_claims(body["access_token"])
        print(json.dumps({k: claims.get(k) for k in KEYS}, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(body, ensure_ascii=False))
