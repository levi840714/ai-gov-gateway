from typing import Any

from mcp.server.fastmcp import FastMCP

from account_ops.state import AccountOps

mcp = FastMCP("account_ops", host="0.0.0.0", port=8200, stateless_http=True)
ops = AccountOps()


@mcp.tool(description="Add an account to the monitoring watchlist.")
def add_to_watchlist(account_id: str, reason: str) -> dict[str, Any]:
    return ops.add_to_watchlist(account_id, reason)


@mcp.tool(description="Freeze an account. amount_usd is the balance to freeze.")
def freeze_account(
    account_id: str, amount_usd: float, counterparty: str, reason: str
) -> dict[str, Any]:
    return ops.freeze_account(account_id, amount_usd, counterparty, reason)


@mcp.tool(description="File a suspicious transaction report (STR) for an account.")
def file_str(account_id: str, summary: str) -> dict[str, Any]:
    return ops.file_str(account_id, summary)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
