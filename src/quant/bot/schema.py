from __future__ import annotations

from functools import lru_cache

from quant.data import schemas


def allowed_tables() -> list[str]:
    """机器人可查询的表白名单（随 schemas 自动同步）。"""
    return sorted({*schemas.TABLES, *schemas.HIT_TABLES})


def _ddl_of(table: str) -> str:
    spec = schemas.TABLES.get(table) or schemas.HIT_TABLES[table]
    return spec.ddl.strip()


@lru_cache(maxsize=1)
def schema_prompt() -> str:
    """喂给大模型的表白名单与建表语句（含列注释）。"""
    return "\n\n".join(f"### {table}\n{_ddl_of(table)}"
                       for table in allowed_tables())
