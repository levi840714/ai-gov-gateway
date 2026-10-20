from dataclasses import dataclass, field
from typing import Any


@dataclass
class AccountOps:
    """模擬的帳戶操作；狀態只在記憶體中，重啟即清空。"""

    watchlist: dict[str, str] = field(default_factory=dict)
    frozen: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: list[dict[str, str]] = field(default_factory=list)

    def add_to_watchlist(self, account_id: str, reason: str) -> dict[str, Any]:
        self.watchlist[account_id] = reason
        return {"account_id": account_id, "status": "watchlisted"}

    def freeze_account(
        self, account_id: str, amount_usd: float, counterparty: str, reason: str
    ) -> dict[str, Any]:
        self.frozen[account_id] = {
            "amount_usd": amount_usd,
            "counterparty": counterparty,
            "reason": reason,
        }
        return {"account_id": account_id, "status": "frozen", "amount_usd": amount_usd}

    def file_str(self, account_id: str, summary: str) -> dict[str, Any]:
        report_id = f"str-{len(self.reports) + 1:04d}"
        self.reports.append({"report_id": report_id, "account_id": account_id, "summary": summary})
        return {"report_id": report_id, "status": "filed"}
