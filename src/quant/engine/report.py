from __future__ import annotations

from pathlib import Path

import pandas as pd


def save_selection(df: pd.DataFrame, trade_date: str, strategy_name: str,
                   out_root: str | Path = "outputs/select") -> Path:
    folder = Path(out_root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{trade_date}_{strategy_name}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path
