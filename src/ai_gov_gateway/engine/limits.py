from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Period = Literal["day", "month"]


class UsageLimits(BaseModel):
    """OPA governance.llm 的 limits；欄位缺漏時驗證失敗，由 policy client 視為 policy_error。"""

    tier: str
    budget_usd: dict[Period, Decimal] = Field(min_length=1)
    rpm: int = Field(gt=0)
    tpm: int = Field(gt=0)
    max_concurrent: int = Field(gt=0)
    max_input_chars: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
