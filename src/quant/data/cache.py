from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from quant.data import db, ingest, schemas


def cache_dir() -> Path:
    return Path(os.environ.get("QUANT_CACHE_DIR", "data_cache"))


def cache_path(table: str) -> Path:
    return cache_dir() / f"{table}.parquet"


def _meta_path(table: str) -> Path:
    return cache_dir() / f"{table}.meta.json"


def _read_meta(table: str) -> dict:
    path = _meta_path(table)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_cache(table: str, df: pd.DataFrame, watermark: str | None) -> Path:
    path = cache_path(table)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    meta = {"watermark": watermark, "rows": len(df)}
    _meta_path(table).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return path


def ensure_cache(table: str) -> Path:
    """比对 MySQL 水位线，必要时拉取增量并重写本地镜像。"""
    watermark = ingest.get_watermark(table)
    path = cache_path(table)
    meta = _read_meta(table)
    if path.exists() and watermark is not None and meta.get("watermark") == watermark:
        return path

    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    date_col = schemas.date_column(table)
    if path.exists() and date_col and meta.get("watermark"):
        delta = db.read_df(
            f"SELECT * FROM `{table}` WHERE `{date_col}` > %s ORDER BY `{date_col}`",
            (meta["watermark"],),
        )
    else:
        delta = db.read_df(f"SELECT * FROM `{table}`")

    if delta.empty:
        delta = pd.DataFrame(columns=schemas.columns_of(table))

    if date_col is None:
        # 非日期表 delta 就是整表快照，直接替换；拼接会把每次快照重复累积
        df = delta
    elif existing.empty:
        df = delta
    elif delta.empty:
        df = existing
    else:
        df = pd.concat([existing, delta], ignore_index=True)
    if df.empty:
        df = pd.DataFrame(columns=schemas.columns_of(table))
    return _write_cache(table, df, watermark)


def ensure_all(tables: list[str] | None = None) -> None:
    names = tables or [n for n in schemas.TABLES if n != "ingest_log"]
    for name in names:
        ensure_cache(name)


def load_table(table: str, columns: list[str] | None = None,
               start: str | None = None, end: str | None = None,
               ts_codes: list[str] | None = None) -> pd.DataFrame:
    path = cache_path(table)
    if not path.exists():
        raise FileNotFoundError(f"缓存缺失：{path}，请先运行 quant data update")

    filters = None
    date_col = schemas.date_column(table)
    if date_col and (start or end):
        filters = []
        if start:
            filters.append((date_col, ">=", start))
        if end:
            filters.append((date_col, "<=", end))

    df = pd.read_parquet(path, columns=columns, filters=filters)
    if df.empty:
        df = pd.DataFrame(
            columns=columns if columns is not None else schemas.columns_of(table)
        )
    if ts_codes is not None and "ts_code" in df.columns:
        df = df[df["ts_code"].isin(ts_codes)].reset_index(drop=True)
    return df
