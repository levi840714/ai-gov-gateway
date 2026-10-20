import json

import httpx

from scripts.env import KEYS_FILE, LITELLM_URL

if __name__ == "__main__":
    key = json.loads(KEYS_FILE.read_text())["rate-probe"]
    for attempt in range(1, 4):
        response = httpx.post(
            f"{LITELLM_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "kyt-main",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 5,
            },
            timeout=60,
        )
        print(attempt, response.status_code, response.text[:160].replace("\n", " "))
