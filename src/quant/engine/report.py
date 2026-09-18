from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd


def save_selection(df: pd.DataFrame, trade_date: str, strategy_name: str,
                   out_root: str | Path = "outputs/select") -> Path:
    folder = Path(out_root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{trade_date}_{strategy_name}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def plot_equity(equity: pd.DataFrame, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    x = pd.to_datetime(equity["trade_date"], format="%Y%m%d")
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x, equity["equity"] / equity["equity"].iloc[0], label="策略净值")
    if "benchmark" in equity.columns and equity["benchmark"].notna().any():
        bench = equity["benchmark"].astype(float).ffill()
        ax.plot(x, bench / bench.dropna().iloc[0], label="基准净值", linestyle="--")
    ax.set_title(title)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def save_backtest(result, metrics: dict,
                  out_root: str | Path = "outputs/backtest") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(out_root) / f"{result.strategy_name}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(folder / "equity.csv", index=False, encoding="utf-8-sig")
    result.trades.to_csv(folder / "trades.csv", index=False, encoding="utf-8-sig")
    (folder / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    plot_equity(result.equity, folder / "equity.png",
                f"{result.strategy_name} 回测净值")
    return folder
