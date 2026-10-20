import json
import sys

from probe_mcp.claims import decode_claims
from scripts.kc import user_token

KEYS = ("iss", "sub", "aud", "azp", "preferred_username", "realm_access", "scope", "act", "exp")

if __name__ == "__main__":
    claims = decode_claims(user_token(sys.argv[1] if len(sys.argv) > 1 else "alice"))
    print(json.dumps({k: claims.get(k) for k in KEYS}, indent=2, ensure_ascii=False))
