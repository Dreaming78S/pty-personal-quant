from __future__ import annotations

import pandas as pd
import pymysql

from quant.config import get_settings

SQL_TIMEOUT_SECONDS = 15


def run_readonly(sql: str, timeout: int = SQL_TIMEOUT_SECONDS) -> pd.DataFrame:
    """在只读事务中执行单条 SELECT，返回结果 DataFrame（报错/超时直接抛出）。"""
    settings = get_settings()
    conn = pymysql.connect(
        host=settings.aliyun_rds_host,
        port=settings.aliyun_rds_port,
        user=settings.aliyun_rds_user,
        password=settings.aliyun_rds_passport,
        database=settings.aliyun_rds_database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=timeout,
        read_timeout=timeout,
        write_timeout=timeout,
    )
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("START TRANSACTION READ ONLY")
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.rollback()
        conn.close()
    return pd.DataFrame(rows)
