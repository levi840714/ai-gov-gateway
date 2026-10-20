import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

KEYCLOAK_TOKEN_URL = "http://localhost:8080/realms/governance/protocol/openid-connect/token"
LITELLM_URL = "http://localhost:4000"
KEYS_FILE = ROOT / "var" / "keys.json"


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing {name} in .env")
    return value


def user_password(username: str) -> str:
    return env(f"{username.upper()}_PASSWORD")
