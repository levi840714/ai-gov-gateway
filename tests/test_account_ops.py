from account_ops.state import AccountOps


def test_operations_update_state():
    ops = AccountOps()
    assert ops.add_to_watchlist("acct-1", "mixer exposure") == {
        "account_id": "acct-1",
        "status": "watchlisted",
    }
    frozen = ops.freeze_account("acct-1", 25000, "0x1", "sanctions")
    assert frozen == {"account_id": "acct-1", "status": "frozen", "amount_usd": 25000}
    assert ops.file_str("acct-1", "summary") == {"report_id": "str-0001", "status": "filed"}
    assert ops.file_str("acct-2", "summary")["report_id"] == "str-0002"
    assert ops.watchlist == {"acct-1": "mixer exposure"}
