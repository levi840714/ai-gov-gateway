from decimal import Decimal

import pytest
from pydantic import ValidationError

from ai_gov_gateway.engine.limits import UsageLimits

VALID = {
    "tier": "default",
    "role": "*",
    "budget_usd": {"day": 2.0},
    "rpm": 20,
    "tpm": 150000,
    "max_concurrent": 4,
    "max_input_chars": 100000,
    "max_output_tokens": 4096,
}


def test_limits_parse_decimal_budget():
    assert UsageLimits.model_validate(VALID).budget_usd == {"day": Decimal("2.0")}


def test_limits_reject_unknown_period():
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, "budget_usd": {"week": 1}})


@pytest.mark.parametrize("field", ["rpm", "tpm", "max_concurrent", "max_input_chars"])
def test_limits_reject_non_positive_counts(field):
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, field: 0})


def test_limits_reject_empty_budget():
    with pytest.raises(ValidationError):
        UsageLimits.model_validate({**VALID, "budget_usd": {}})
