"""策略命中两日持有表现报告：T+1 开盘买入、T+2 收盘卖出。

用法：
    uv run python scripts/hits_performance_report.py --start 2026-01-01
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from quant.data import db, schemas
from quant.engine import hit_performance, loader
from quant.utils.dates import to_yyyymmdd

BUCKETS = hit_performance.BUCKET_NAMES
PLAIN_COLUMNS = ("策略", "推荐", "可计算", "停牌跳过", "数据不足")


def _cell(count: int, total: int) -> str:
    pct = f"{count / total * 100:.2f}%" if total else "-"
    return f"{count} ({pct})"


def _strategy_row(market: pd.DataFrame, strategy: str,
                  hits: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    forward = hit_performance.forward_returns(market, hits)
    stats = hit_performance.bucket_stats(
        forward.loc[forward["status"] == "ok", "ret_pct"])
    line = {
        "策略": strategy,
        "推荐": len(hits),
        "可计算": stats["可计算"],
        "停牌跳过": int((forward["status"] == "suspended").sum()),
        "数据不足": int((forward["status"] == "no_data").sum()),
        "盈利": stats["盈利"],
        **{b: stats[b] for b in BUCKETS},
    }
    forward.insert(0, "strategy", strategy)
    return line, forward


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-01-01", help="命中起始日期")
    parser.add_argument("--end", default=None, help="结束日期，缺省最新交易日")
    parser.add_argument("--output-dir", default="outputs/reports")
    args = parser.parse_args()

    start = to_yyyymmdd(args.start)
    end = to_yyyymmdd(args.end) if args.end else loader.resolve_trade_date()

    market = loader.load_market_data(start, end, warmup_days=0)
    if market.empty:
        raise SystemExit(f"{start}~{end} 区间内没有行情数据")

    lines, details = [], []
    for strategy in schemas.HIT_STRATEGIES:
        hits = db.read_df(
            f"SELECT ts_code, trade_date FROM `{schemas.hit_table_name(strategy)}` "
            "WHERE trade_date>=%s AND trade_date<=%s",
            (start, end),
        )
        if hits.empty:
            continue
        line, forward = _strategy_row(market, strategy, hits)
        lines.append(line)
        details.append(forward)

    if not lines:
        raise SystemExit("区间内没有任何命中记录")

    table = pd.DataFrame(lines)
    table = pd.concat([table, pd.DataFrame([{
        "策略": "合计",
        "推荐": int(table["推荐"].sum()),
        "可计算": int(table["可计算"].sum()),
        "停牌跳过": int(table["停牌跳过"].sum()),
        "数据不足": int(table["数据不足"].sum()),
        "盈利": int(table["盈利"].sum()),
        **{b: int(table[b].sum()) for b in BUCKETS},
    }])], ignore_index=True)

    detail = pd.concat(details, ignore_index=True)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{start}_{end}"
    md_path = out_dir / f"hits_performance_{stamp}.md"
    csv_path = out_dir / f"hits_forward_returns_{stamp}.csv"
    detail.to_csv(csv_path, index=False, encoding="utf-8-sig")

    header = [
        "# 策略命中两日持有表现报告",
        "",
        f"- 区间：{start} ~ {end}（按命中日 T 起算）",
        "- 规则：T 日命中 → T+1 开盘价买入 → T+2 收盘价卖出（不复权价，即行情界面真实成交价；持有期内分红现金未计入）",
        "- 收益率 =（T+2 收盘 − T+1 开盘）/ T+1 开盘 × 100%；>0 记盈利，≤0 记亏",
        "- T+1/T+2 为交易日历上紧邻的两个交易日；买入日或卖出日无行情（停牌）跳过，"
        "T+2 超出数据范围记数据不足",
        "- 分桶互斥：<-5；-5~-2（含 -5、-2）；-2~0（含 0）；0~2；2~5；>5（含 5）；"
        "括号内百分比 = 占可计算次数",
        "",
    ]
    body = ["| " + " | ".join(table.columns) + " |",
            "|" + "---|" * len(table.columns)]
    for _, row in table.iterrows():
        cells = []
        for col in table.columns:
            if col in PLAIN_COLUMNS:
                cells.append(str(row[col]) if col == "策略" else f"{int(row[col])}")
            else:
                cells.append(_cell(int(row[col]), int(row["可计算"])))
        body.append("| " + " | ".join(cells) + " |")

    md_path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    print(f"报告：{md_path}")
    print(f"明细：{csv_path}")


if __name__ == "__main__":
    main()
