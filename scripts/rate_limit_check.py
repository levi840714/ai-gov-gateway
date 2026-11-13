"""確認 key 的 rpm 限制：對 probe 模型送出 rpm_limit + 1 次，最後一次應回 429，不花 LLM 費用。

M5 起每位使用者另有個人 rpm（OPA 的等級），請求分給三位測試帳號，個人都不超過自己的上限。
"""

from collections import Counter

from scripts.kc import user_token
from scripts.llm_sim import AGENT_KEY, LIMIT_MODEL, chat, key_limited, wait_until_allowed
from scripts.make_keys import KEYS

# 個人 rpm：alice 20（default）、bob 與 carol 30（supervisor）
SHARES = {"bob": 25, "carol": 25, "alice": 15}

if __name__ == "__main__":
    limit = KEYS[AGENT_KEY]["rpm_limit"]
    users = [user for user, share in SHARES.items() for _ in range(share)][: limit + 1]
    if len(users) <= limit:
        raise SystemExit(f"SHARES must cover rpm_limit + 1 = {limit + 1} requests")
    messages = [{"role": "system", "content": "probe:reply=ok"}, {"role": "user", "content": "Hi"}]
    wait_until_allowed(AGENT_KEY)
    tokens = {user: user_token(user) for user in SHARES}
    responses = [chat(AGENT_KEY, messages, model=LIMIT_MODEL, token=tokens[user]) for user in users]
    statuses = [r.status_code for r in responses]
    print(
        f"{AGENT_KEY} rpm_limit={limit}:",
        dict(Counter(statuses)),
        "last:",
        statuses[-1],
        "key limited:",
        key_limited(responses[-1]),
    )
    print("waiting for the rate limit window to pass...")
    wait_until_allowed(AGENT_KEY)
