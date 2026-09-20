from quant.bot import schema
from quant.data import schemas


def test_allowed_tables_covers_all_schema_tables():
    names = schema.allowed_tables()

    assert names == sorted({*schemas.TABLES, *schemas.HIT_TABLES})
    assert "ingest_log" in names
    assert "hit_rise_shrink_pullback" in names
    assert len(names) == 20


def test_schema_prompt_contains_every_table_and_columns():
    text = schema.schema_prompt()

    for name in schema.allowed_tables():
        assert f"### {name}" in text
    assert "trade_date" in text
    assert "raw_close" in text
    assert "不复权收盘价" in text


def test_schema_prompt_is_cached():
    assert schema.schema_prompt() is schema.schema_prompt()
