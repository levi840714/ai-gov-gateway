import httpx

from scripts.env import KEYCLOAK_TOKEN_URL, env, user_password


def user_token(username: str, client_id: str = "kyt-cli") -> str:
    """Spike 用 password grant 取代 device flow，測試帳號密碼來自 realm 檔。"""
    response = httpx.post(
        KEYCLOAK_TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": user_password(username),
            "scope": "openid",
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def client_token(client_id: str, secret_env: str) -> str:
    response = httpx.post(
        KEYCLOAK_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": env(secret_env),
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])
