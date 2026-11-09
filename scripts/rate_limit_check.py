"""確認 key 的 rpm 限制：對 probe 模型送出 rpm_limit + 1 次，最後一次應回 429，不花 LLM 費用。"""

from collections import Counter

from scripts.llm_sim import AGENT_KEY, chat, wait_until_allowed
from scripts.make_keys import KEYS

if __name__ == "__main__":
    limit = KEYS[AGENT_KEY]["rpm_limit"]
    messages = [{"role": "system", "content": "probe:reply=ok"}, {"role": "user", "content": "Hi"}]
    wait_until_allowed(AGENT_KEY)
    statuses = [chat(AGENT_KEY, messages).status_code for _ in range(limit + 1)]
    print(f"{AGENT_KEY} rpm_limit={limit}:", dict(Counter(statuses)), "last:", statuses[-1])
    print("waiting for the rate limit window to pass...")
    wait_until_allowed(AGENT_KEY)
