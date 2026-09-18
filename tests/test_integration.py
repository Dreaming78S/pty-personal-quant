import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION"),
                       reason="需要 RUN_INTEGRATION=1 才运行集成测试"),
]


def test_mysql_core_tables_reachable():
    from quant.data import db

    count = db.scalar("SELECT COUNT(*) FROM trade_cal")
    assert count and count > 0


def test_tushare_fetch_trade_cal():
    from quant.data.tushare_client import TushareClient

    df = TushareClient().fetch_trade_cal("20240101", "20240110")
    assert not df.empty
