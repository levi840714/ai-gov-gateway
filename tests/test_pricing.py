from pathlib import Path

import pytest
import yaml

from ai_gov_gateway.engine.pricing import priced_aliases

MODEL_LIST = [
    {"model_name": "kyt-main", "litellm_params": {"model": "gemini/gemini-3.8-flash"}},
    {"model_name": "kyt-main-fallback", "litellm_params": {"model": "openai/gpt-6-luna"}},
    {
        "model_name": "probe-primary",
        "litellm_params": {"model": "openai/probe-primary"},
        "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-5},
    },
    {"model_name": "unpriced", "litellm_params": {"model": "openai/nope"}},
]
COSTS = {
    "gemini/gemini-3.8-flash": {"input_cost_per_token": 7.5e-7, "output_cost_per_token": 3.75e-6},
    "gpt-6-luna": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
}


def test_alias_and_fallbacks_priced():
    priced = priced_aliases(MODEL_LIST, [{"kyt-main": ["kyt-main-fallback"]}], COSTS)
    assert {"kyt-main", "kyt-main-fallback", "probe-primary"} <= priced


def test_provider_prefix_is_stripped_when_not_found():
    assert "kyt-main-fallback" in priced_aliases(MODEL_LIST, [], COSTS)


def test_model_info_counts_as_price():
    assert "probe-primary" in priced_aliases(MODEL_LIST, [], {})


def test_unpriced_alias_excluded():
    assert "unpriced" not in priced_aliases(MODEL_LIST, [], COSTS)


def test_unpriced_fallback_excludes_alias():
    # fallback 沒有單價時那次呼叫的成本為 0，整個別名視為沒有單價
    assert "kyt-main" not in priced_aliases(MODEL_LIST, [{"kyt-main": ["unpriced"]}], COSTS)


def test_unpriced_fallback_of_fallback_excludes_alias():
    fallbacks = [{"kyt-main": ["kyt-main-fallback"]}, {"kyt-main-fallback": ["unpriced"]}]
    assert "kyt-main" not in priced_aliases(MODEL_LIST, fallbacks, COSTS)


def test_unknown_fallback_alias_excludes_alias():
    assert "kyt-main" not in priced_aliases(MODEL_LIST, [{"kyt-main": ["missing"]}], COSTS)


def test_fallback_cycle_terminates():
    fallbacks = [{"kyt-main": ["kyt-main-fallback"]}, {"kyt-main-fallback": ["kyt-main"]}]
    priced = priced_aliases(MODEL_LIST, fallbacks, COSTS)
    assert {"kyt-main", "kyt-main-fallback"} <= priced


def test_every_deployment_of_an_alias_needs_a_price():
    model_list = [*MODEL_LIST, {"model_name": "kyt-main", "litellm_params": {"model": "x/nope"}}]
    assert "kyt-main" not in priced_aliases(model_list, [], COSTS)


@pytest.mark.parametrize(
    "info",
    [
        {"input_cost_per_token": 1e-6},
        {"input_cost_per_token": 1e-6, "output_cost_per_token": None},
        {"input_cost_per_token": 0, "output_cost_per_token": 1e-5},
        {"input_cost_per_token": True, "output_cost_per_token": 1e-5},
    ],
)
def test_incomplete_or_zero_prices_do_not_count(info):
    model_list = [{"model_name": "m", "litellm_params": {"model": "openai/m"}, "model_info": info}]
    assert "m" not in priced_aliases(model_list, [], {})


def test_gateway_config_prices_probe_models():
    config = yaml.safe_load(Path("litellm/config.yaml").read_text())
    priced = priced_aliases(config["model_list"], config["router_settings"]["fallbacks"], COSTS)
    assert {"kyt-main", "kyt-main-fallback", "probe-primary", "probe-fallback"} <= priced
