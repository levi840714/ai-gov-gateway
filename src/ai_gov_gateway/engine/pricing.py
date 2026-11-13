"""放行前確認模型別名有單價：沒有單價時 LiteLLM 算出的成本是 0，事後累計的預算形同虛設。

只收 plain data，不 import LiteLLM；plugin 以 router 的 model_list、fallbacks
與 litellm.model_cost 建立。
"""

from collections.abc import Mapping, Sequence
from typing import Any

PRICE_KEYS = ("input_cost_per_token", "output_cost_per_token")


def priced_aliases(
    model_list: Sequence[Mapping[str, Any]],
    fallbacks: Sequence[Mapping[str, Any]] | None,
    model_cost: Mapping[str, Any],
) -> frozenset[str]:
    """別名的每個 deployment 都有輸入與輸出單價，且 fallback 鏈上的別名也都符合，才列入。"""
    deployments: dict[str, list[bool]] = {}
    for deployment in model_list:
        alias = deployment.get("model_name")
        if isinstance(alias, str):
            deployments.setdefault(alias, []).append(_deployment_priced(deployment, model_cost))
    # 不存在的 fallback 別名沒有 deployment，視為沒有單價
    own = {alias for alias, priced in deployments.items() if all(priced)}
    chains = _fallback_map(fallbacks or [])
    return frozenset(alias for alias in own if _reachable(alias, chains) <= own)


def _deployment_priced(deployment: Mapping[str, Any], model_cost: Mapping[str, Any]) -> bool:
    if _has_prices(deployment.get("model_info")):
        return True
    params = deployment.get("litellm_params")
    model = params.get("model") if isinstance(params, Mapping) else None
    if not isinstance(model, str):
        return False
    if _has_prices(model_cost.get(model)):
        return True
    # litellm.model_cost 的 OpenAI 等模型不含 provider 前綴，例如 gpt-6-luna
    _, slash, bare = model.partition("/")
    return bool(slash) and _has_prices(model_cost.get(bare))


def _has_prices(info: Any) -> bool:
    if not isinstance(info, Mapping):
        return False
    prices = [info.get(key) for key in PRICE_KEYS]
    return all(
        isinstance(price, int | float) and not isinstance(price, bool) and price > 0
        for price in prices
    )


def _fallback_map(fallbacks: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    chains: dict[str, list[str]] = {}
    for entry in fallbacks:
        if not isinstance(entry, Mapping):
            continue
        for alias, targets in entry.items():
            if isinstance(targets, list):
                chains.setdefault(alias, []).extend(t for t in targets if isinstance(t, str))
    return chains


def _reachable(alias: str, chains: Mapping[str, list[str]]) -> set[str]:
    """別名本身與 fallback 鏈上的所有別名；以 seen 防止循環。"""
    seen = {alias}
    pending = [alias]
    while pending:
        for target in chains.get(pending.pop(), []):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen
