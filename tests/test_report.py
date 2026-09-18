import pandas as pd

from quant.engine import report


def test_save_selection_writes_utf8_csv(tmp_path):
    df = pd.DataFrame({"rank": [1], "ts_code": ["600000.SH"], "name": ["浦发银行"]})
    path = report.save_selection(df, "20240103", "ma_volume", out_root=tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8-sig")
    assert "浦发银行" in text
    assert path.name == "20240103_ma_volume.csv"
