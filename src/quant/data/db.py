from __future__ import annotations

from collections.abc import Iterable, Sequence

import pandas as pd
import pymysql

from quant.config import get_settings


def get_connection() -> pymysql.connections.Connection:
    s = get_settings()
    return pymysql.connect(
        host=s.aliyun_rds_host,
        port=s.aliyun_rds_port,
        user=s.aliyun_rds_user,
        password=s.aliyun_rds_passport,
        database=s.aliyun_rds_database,
        charset="utf8mb4",
        autocommit=False,
    )


def build_upsert_sql(table: str, columns: list[str]) -> str:
    cols = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in columns)
    return (
        f"INSERT INTO `{table}` ({cols}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


def upsert_rows(table: str, columns: list[str], rows: Iterable[Sequence],
                chunk_size: int = 2000) -> int:
    sql = build_upsert_sql(table, columns)
    total = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            batch: list[tuple] = []
            for row in rows:
                batch.append(tuple(row))
                if len(batch) >= chunk_size:
                    total += cur.executemany(sql, batch)
                    batch = []
            if batch:
                total += cur.executemany(sql, batch)
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_df(table: str, df: pd.DataFrame, columns: list[str] | None = None) -> int:
    cols = list(columns) if columns is not None else list(df.columns)
    data = df[cols].astype(object).where(pd.notna(df[cols]), None)
    rows = data.itertuples(index=False, name=None)
    return upsert_rows(table, cols, rows)


def read_df(sql: str, params: Sequence | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.DataFrame(rows)


def scalar(sql: str, params: Sequence | None = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def execute(sql: str, params: Sequence | None = None) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            n = cur.execute(sql, params)
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
