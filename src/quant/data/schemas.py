from __future__ import annotations

from dataclasses import dataclass

from quant.data import db


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    ddl: str
    date_column: str | None = "trade_date"


TABLES: dict[str, TableSpec] = {
    "stock_basic": TableSpec(
        name="stock_basic",
        columns=("ts_code", "symbol", "name", "area", "industry", "market",
                 "exchange", "list_status", "list_date", "delist_date", "is_hs"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS stock_basic (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  symbol VARCHAR(10) NOT NULL COMMENT '股票代码',
  name VARCHAR(32) NOT NULL COMMENT '名称',
  area VARCHAR(16) COMMENT '地域',
  industry VARCHAR(32) COMMENT '行业',
  market VARCHAR(16) COMMENT '市场(主板/创业板/科创板/北交所)',
  exchange VARCHAR(8) COMMENT '交易所',
  list_status CHAR(1) COMMENT 'L上市 D退市 P暂停',
  list_date CHAR(8) COMMENT '上市日期',
  delist_date CHAR(8) COMMENT '退市日期',
  is_hs CHAR(1) COMMENT '是否沪深港通标的',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息'
""",
    ),
    "trade_cal": TableSpec(
        name="trade_cal",
        columns=("exchange", "cal_date", "is_open", "pretrade_date"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS trade_cal (
  exchange VARCHAR(8) NOT NULL COMMENT '交易所',
  cal_date CHAR(8) NOT NULL COMMENT '日历日期',
  is_open TINYINT NOT NULL COMMENT '是否交易 1开市 0休市',
  pretrade_date CHAR(8) COMMENT '上一个交易日',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (exchange, cal_date),
  KEY idx_cal_date (cal_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易日历'
""",
    ),
    "daily": TableSpec(
        name="daily",
        columns=("ts_code", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"),
        ddl="""
CREATE TABLE IF NOT EXISTS daily (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  open DECIMAL(12,4) COMMENT '开盘价',
  high DECIMAL(12,4) COMMENT '最高价',
  low DECIMAL(12,4) COMMENT '最低价',
  close DECIMAL(12,4) COMMENT '收盘价',
  pre_close DECIMAL(12,4) COMMENT '昨收价',
  `change` DECIMAL(12,4) COMMENT '涨跌额',
  pct_chg DECIMAL(10,4) COMMENT '涨跌幅%',
  vol DECIMAL(20,2) COMMENT '成交量(手)',
  amount DECIMAL(20,4) COMMENT '成交额(千元)',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情'
""",
    ),
    "adj_factor": TableSpec(
        name="adj_factor",
        columns=("ts_code", "trade_date", "adj_factor"),
        ddl="""
CREATE TABLE IF NOT EXISTS adj_factor (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  adj_factor DECIMAL(20,8) COMMENT '复权因子',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='复权因子'
""",
    ),
    "daily_basic": TableSpec(
        name="daily_basic",
        columns=("ts_code", "trade_date", "turnover_rate", "turnover_rate_f",
                 "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
                 "dv_ratio", "dv_ttm", "total_share", "float_share",
                 "free_share", "total_mv", "circ_mv"),
        ddl="""
CREATE TABLE IF NOT EXISTS daily_basic (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  turnover_rate DECIMAL(16,6) COMMENT '换手率%',
  turnover_rate_f DECIMAL(16,6) COMMENT '自由流通换手率%',
  volume_ratio DECIMAL(16,6) COMMENT '量比',
  pe DECIMAL(16,6) COMMENT '市盈率',
  pe_ttm DECIMAL(16,6) COMMENT '市盈率TTM',
  pb DECIMAL(16,6) COMMENT '市净率',
  ps DECIMAL(16,6) COMMENT '市销率',
  ps_ttm DECIMAL(16,6) COMMENT '市销率TTM',
  dv_ratio DECIMAL(16,6) COMMENT '股息率%',
  dv_ttm DECIMAL(16,6) COMMENT '股息率TTM%',
  total_share DECIMAL(20,4) COMMENT '总股本(万股)',
  float_share DECIMAL(20,4) COMMENT '流通股本(万股)',
  free_share DECIMAL(20,4) COMMENT '自由流通股本(万股)',
  total_mv DECIMAL(20,4) COMMENT '总市值(万元)',
  circ_mv DECIMAL(20,4) COMMENT '流通市值(万元)',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日指标'
""",
    ),
    "suspend_d": TableSpec(
        name="suspend_d",
        columns=("ts_code", "trade_date", "suspend_timing", "suspend_type"),
        ddl="""
CREATE TABLE IF NOT EXISTS suspend_d (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  suspend_timing VARCHAR(16) COMMENT '日内停牌时段',
  suspend_type VARCHAR(8) COMMENT 'S停牌 R复牌',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日停牌信息'
""",
    ),
    "stk_limit": TableSpec(
        name="stk_limit",
        columns=("ts_code", "trade_date", "up_limit", "down_limit"),
        ddl="""
CREATE TABLE IF NOT EXISTS stk_limit (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  up_limit DECIMAL(12,4) COMMENT '涨停价',
  down_limit DECIMAL(12,4) COMMENT '跌停价',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日涨跌停价格'
""",
    ),
    "index_daily": TableSpec(
        name="index_daily",
        columns=("ts_code", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"),
        ddl="""
CREATE TABLE IF NOT EXISTS index_daily (
  ts_code VARCHAR(12) NOT NULL COMMENT '指数代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  open DECIMAL(12,4) COMMENT '开盘点位',
  high DECIMAL(12,4) COMMENT '最高点位',
  low DECIMAL(12,4) COMMENT '最低点位',
  close DECIMAL(12,4) COMMENT '收盘点位',
  pre_close DECIMAL(12,4) COMMENT '昨收点位',
  `change` DECIMAL(12,4) COMMENT '涨跌点位',
  pct_chg DECIMAL(10,4) COMMENT '涨跌幅%',
  vol DECIMAL(20,2) COMMENT '成交量(手)',
  amount DECIMAL(20,4) COMMENT '成交额(千元)',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数日线'
""",
    ),
    "namechange": TableSpec(
        name="namechange",
        columns=("ts_code", "name", "start_date", "end_date", "ann_date",
                 "change_reason"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS namechange (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  name VARCHAR(32) NOT NULL COMMENT '证券名称',
  start_date CHAR(8) COMMENT '开始日期',
  end_date CHAR(8) COMMENT '结束日期',
  ann_date CHAR(8) COMMENT '公告日期',
  change_reason VARCHAR(64) COMMENT '变更原因',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, start_date),
  KEY idx_start_date (start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票名称变更'
""",
    ),
    "ingest_log": TableSpec(
        name="ingest_log",
        columns=("task_name", "last_trade_date"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS ingest_log (
  task_name VARCHAR(32) NOT NULL COMMENT '任务名(表名)',
  last_trade_date CHAR(8) COMMENT '已入库最大日期',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='入库水位线'
""",
    ),
}


def columns_of(table: str) -> list[str]:
    return list(TABLES[table].columns)


def date_column(table: str) -> str | None:
    return TABLES[table].date_column


def create_all() -> list[str]:
    for spec in TABLES.values():
        db.execute(spec.ddl)
    return list(TABLES)
