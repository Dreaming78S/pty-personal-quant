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
                 "free_share", "total_mv", "circ_mv", "limit_status"),
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
  limit_status INT NULL COMMENT '收盘涨跌状态：0平盘,1涨,2涨停,3一字涨停,4跌,5跌停,6一字跌停',
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
  suspend_timing VARCHAR(255) NOT NULL DEFAULT '' COMMENT '日内停牌时段',
  suspend_type VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'S停牌 R复牌',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date, suspend_type, suspend_timing),
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
  change_reason VARCHAR(255) COMMENT '变更原因',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, start_date),
  KEY idx_start_date (start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票名称变更'
""",
    ),
    "stock_company": TableSpec(
        name="stock_company",
        columns=("ts_code", "com_name", "com_id", "exchange", "chairman",
                 "manager", "secretary", "reg_capital", "setup_date",
                 "province", "city", "introduction", "website", "email",
                 "office", "employees", "main_business", "business_scope"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS stock_company (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  com_name VARCHAR(64) COMMENT '公司名称',
  com_id VARCHAR(32) COMMENT '统一社会信用代码',
  exchange VARCHAR(8) COMMENT '交易所',
  chairman VARCHAR(64) COMMENT '董事长',
  manager VARCHAR(64) COMMENT '总经理',
  secretary VARCHAR(64) COMMENT '董秘',
  reg_capital DECIMAL(20,4) COMMENT '注册资本(万元)',
  setup_date CHAR(8) COMMENT '注册日期',
  province VARCHAR(32) COMMENT '省份',
  city VARCHAR(32) COMMENT '城市',
  introduction MEDIUMTEXT COMMENT '公司介绍',
  website VARCHAR(128) COMMENT '公司主页',
  email VARCHAR(128) COMMENT '电子邮件',
  office VARCHAR(128) COMMENT '办公室',
  employees INT COMMENT '员工人数',
  main_business MEDIUMTEXT COMMENT '主要业务及产品',
  business_scope MEDIUMTEXT COMMENT '经营范围',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='上市公司基本信息'
""",
    ),
    "new_share": TableSpec(
        name="new_share",
        columns=("ts_code", "sub_code", "name", "ipo_date", "issue_date",
                 "amount", "market_amount", "price", "pe", "limit_amount",
                 "funds", "ballot"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS new_share (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  sub_code VARCHAR(10) COMMENT '申购代码',
  name VARCHAR(32) COMMENT '股票名称',
  ipo_date CHAR(8) COMMENT '上网发行日期',
  issue_date CHAR(8) COMMENT '上市日期',
  amount DECIMAL(20,4) COMMENT '发行总量(万股)',
  market_amount DECIMAL(20,4) COMMENT '上网发行总量(万股)',
  price DECIMAL(12,4) COMMENT '发行价格',
  pe DECIMAL(16,6) COMMENT '发行市盈率',
  limit_amount DECIMAL(20,4) COMMENT '个人申购上限(万股)',
  funds DECIMAL(20,4) COMMENT '募集资金(亿元)',
  ballot DECIMAL(12,6) COMMENT '中签率',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='IPO新股列表'
""",
    ),
    "stk_holdertrade": TableSpec(
        name="stk_holdertrade",
        columns=("ts_code", "ann_date", "holder_name", "holder_type", "in_de",
                 "change_vol", "change_ratio", "after_share", "after_ratio",
                 "avg_price", "total_share", "begin_date", "close_date"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS stk_holdertrade (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  ann_date CHAR(8) NOT NULL COMMENT '公告日期',
  holder_name VARCHAR(255) NOT NULL COMMENT '股东名称',
  holder_type CHAR(2) COMMENT '股东类型G高管P个人C公司',
  in_de CHAR(2) NOT NULL COMMENT '增减持类型IN增持DE减持',
  change_vol DECIMAL(20,4) NOT NULL COMMENT '变动数量(万股)',
  change_ratio DECIMAL(12,4) COMMENT '占流通比例%',
  after_share DECIMAL(20,4) COMMENT '变动后持股(万股)',
  after_ratio DECIMAL(12,4) COMMENT '变动后占流通比例%',
  avg_price DECIMAL(12,4) COMMENT '平均价格',
  total_share DECIMAL(20,4) COMMENT '持股总数(万股)',
  begin_date CHAR(8) COMMENT '增减持开始日期',
  close_date CHAR(8) COMMENT '增减持结束日期',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, ann_date, holder_name, in_de, change_vol),
  KEY idx_ann_date (ann_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股东增减持'
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


HIT_STRATEGIES: tuple[str, ...] = (
    "ma_volume", "turtle_trade", "high_tight_flag", "limit_up_shakeout",
    "uptrend_limit_down", "rps_breakout",
)

HIT_COLUMNS: tuple[str, ...] = (
    "ts_code", "trade_date", "rank", "name", "industry", "close",
    "raw_close", "amount", "score", "prev_hit", "hit_3d", "hit_5d",
    "hit_10d", "streak", "params",
)

# 已有 hit 表补齐新列用（按交易日历回看，不含当日；streak 含当日）。
HIT_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("industry", "VARCHAR(32) COMMENT '所属行业(入库时快照)' AFTER `name`"),
    ("prev_hit", "TINYINT NOT NULL DEFAULT 0 COMMENT '上一交易日同策略是否命中' AFTER `score`"),
    ("hit_3d", "INT NOT NULL DEFAULT 0 COMMENT '前3个交易日同策略命中天数' AFTER `prev_hit`"),
    ("hit_5d", "INT NOT NULL DEFAULT 0 COMMENT '前5个交易日同策略命中天数' AFTER `hit_3d`"),
    ("hit_10d", "INT NOT NULL DEFAULT 0 COMMENT '前10个交易日同策略命中天数' AFTER `hit_5d`"),
    ("streak", "INT NOT NULL DEFAULT 0 COMMENT '同策略连续命中天数(含当日)' AFTER `hit_10d`"),
)


def hit_table_name(strategy: str) -> str:
    return f"hit_{strategy}"


def _hit_ddl(table: str, strategy: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS `{table}` (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '信号日期',
  `rank` INT COMMENT '当日命中排名(按score降序)',
  name VARCHAR(32) COMMENT '股票名称(入库时快照)',
  industry VARCHAR(32) COMMENT '所属行业(入库时快照)',
  close DECIMAL(12,4) COMMENT '后复权收盘价',
  raw_close DECIMAL(12,4) COMMENT '不复权收盘价',
  amount DECIMAL(20,4) COMMENT '成交额(千元)',
  score DECIMAL(20,6) COMMENT '排序分(该策略口径)',
  prev_hit TINYINT NOT NULL DEFAULT 0 COMMENT '上一交易日同策略是否命中',
  hit_3d INT NOT NULL DEFAULT 0 COMMENT '前3个交易日同策略命中天数',
  hit_5d INT NOT NULL DEFAULT 0 COMMENT '前5个交易日同策略命中天数',
  hit_10d INT NOT NULL DEFAULT 0 COMMENT '前10个交易日同策略命中天数',
  streak INT NOT NULL DEFAULT 0 COMMENT '同策略连续命中天数(含当日)',
  params VARCHAR(512) COMMENT '策略参数JSON快照',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '首次入库时间',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='{strategy}历史命中'
"""


# 派生的策略命中表：不参与 Tushare 抓取、缓存镜像与夜间重建清空。
HIT_TABLES: dict[str, TableSpec] = {
    hit_table_name(strategy): TableSpec(
        name=hit_table_name(strategy),
        columns=HIT_COLUMNS,
        ddl=_hit_ddl(hit_table_name(strategy), strategy),
        date_column="trade_date",
    )
    for strategy in HIT_STRATEGIES
}


def columns_of(table: str) -> list[str]:
    if table in HIT_TABLES:
        return list(HIT_TABLES[table].columns)
    return list(TABLES[table].columns)


def create_hit_tables() -> list[str]:
    for spec in HIT_TABLES.values():
        db.execute(spec.ddl)
    return list(HIT_TABLES)


def date_column(table: str) -> str | None:
    return TABLES[table].date_column


def create_all() -> list[str]:
    for spec in TABLES.values():
        db.execute(spec.ddl)
    return list(TABLES)


MIGRATIONS: list[tuple[str, str, str]] = [
    ("daily_basic", "limit_status",
     "ALTER TABLE `daily_basic` ADD COLUMN `limit_status` INT NULL COMMENT '收盘涨跌状态：0平盘,1涨,2涨停,3一字涨停,4跌,5跌停,6一字跌停' AFTER `circ_mv`"),
]

WIDENINGS: list[tuple[str, str, int, str]] = [
    ("stk_holdertrade", "holder_name", 255,
     "ALTER TABLE `stk_holdertrade` MODIFY COLUMN `holder_name` VARCHAR(255) NOT NULL COMMENT '股东名称'"),
    ("suspend_d", "suspend_timing", 255,
     "ALTER TABLE `suspend_d` MODIFY COLUMN `suspend_timing` VARCHAR(255) COMMENT '日内停牌时段'"),
    ("namechange", "change_reason", 255,
     "ALTER TABLE `namechange` MODIFY COLUMN `change_reason` VARCHAR(255) COMMENT '变更原因'"),
]

CONDITIONAL_MIGRATIONS: list[tuple[str, str, list[str]]] = [
    ("suspend_d_pk",
     "SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
     "AND TABLE_NAME='suspend_d' AND INDEX_NAME='PRIMARY' AND COLUMN_NAME='suspend_timing'",
     [
         "UPDATE `suspend_d` SET `suspend_type`='' WHERE `suspend_type` IS NULL",
         "UPDATE `suspend_d` SET `suspend_timing`='' WHERE `suspend_timing` IS NULL",
         "ALTER TABLE `suspend_d` MODIFY COLUMN `suspend_type` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'S停牌 R复牌'",
         "ALTER TABLE `suspend_d` MODIFY COLUMN `suspend_timing` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '日内停牌时段'",
         "ALTER TABLE `suspend_d` DROP PRIMARY KEY, ADD PRIMARY KEY (`ts_code`,`trade_date`,`suspend_type`,`suspend_timing`)",
     ]),
]


def _column_length(df) -> int | None:
    if df.empty or "CHARACTER_MAXIMUM_LENGTH" not in df.columns:
        return None
    value = df.iloc[0]["CHARACTER_MAXIMUM_LENGTH"]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _table_exists(table: str) -> bool:
    df = db.read_df(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return not df.empty


def migrate() -> list[str]:
    """对已存在的表补齐缺失列、加宽过窄的列并执行条件迁移（幂等）。表不存在（如全新库）时跳过，返回实际执行的 <表>.<列> 列表。"""
    applied = []
    for table, column, sql in MIGRATIONS:
        if not _table_exists(table):
            continue
        df = db.read_df(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        )
        if not df.empty:
            continue
        db.execute(sql)
        applied.append(f"{table}.{column}")
    for table, column, min_length, sql in WIDENINGS:
        if not _table_exists(table):
            continue
        df = db.read_df(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        )
        current = _column_length(df)
        if current is None or current >= min_length:
            continue
        db.execute(sql)
        applied.append(f"{table}.{column}")
    for name, check_sql, statements in CONDITIONAL_MIGRATIONS:
        table = name.removesuffix("_pk")
        if not _table_exists(table):
            continue
        df = db.read_df(check_sql)
        try:
            already_applied = int(df.iloc[0, 0]) if not df.empty else 0
        except (TypeError, ValueError):
            already_applied = 0
        if already_applied > 0:
            continue
        for sql in statements:
            db.execute(sql)
        applied.append(f"{table}.pk")
    return applied


def migrate_hit_columns() -> list[str]:
    """为已存在的 hit 表补齐新增列（幂等），返回实际执行的 <表>.<列> 列表。"""
    applied = []
    for table in HIT_TABLES:
        if not _table_exists(table):
            continue
        for column, definition in HIT_ADDED_COLUMNS:
            df = db.read_df(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                (table, column),
            )
            if not df.empty:
                continue
            db.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
            applied.append(f"{table}.{column}")
    return applied
