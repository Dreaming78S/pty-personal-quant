"""多策略共振表现报告：同一天被 ≥2 个策略命中的股票，T+1 开盘买入、T+2 收盘卖出。

用法：
    uv run python scripts/multi_strategy_performance_report.py --start 2026-01-01
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from quant.data import db, schemas
from quant.engine import hit_performance, loader
from quant.utils.dates import to_yyyymmdd

BUCKETS = hit_performance.BUCKET_NAMES
PLAIN_COLUMNS = ("名称", "事件数", "可计算", "停牌跳过", "数据不足")
LOOKBACK_DAYS = 40


def _shift_days(yyyymmdd: str, days: int) -> str:
    return (datetime.strptime(yyyymmdd, "%Y%m%d")
            + timedelta(days=days)).strftime("%Y%m%d")


def _cell(count: int, total: int) -> str:
    pct = f"{count / total * 100:.2f}%" if total else "-"
    return f"{count} ({pct})"


def _summary(label: str, forward: pd.DataFrame) -> dict:
    stats = hit_performance.bucket_stats(
        forward.loc[forward["status"] == "ok", "ret_pct"])
    return {
        "名称": label,
        "事件数": len(forward),
        "可计算": stats["可计算"],
        "停牌跳过": int((forward["status"] == "suspended").sum()),
        "数据不足": int((forward["status"] == "no_data").sum()),
        "盈利": stats["盈利"],
        **{b: stats[b] for b in BUCKETS},
    }


def _table_md(frame: pd.DataFrame) -> list[str]:
    lines = ["| " + " | ".join(frame.columns) + " |",
             "|" + "---|" * len(frame.columns)]
    for _, row in frame.iterrows():
        cells = []
        for col in frame.columns:
            if col in PLAIN_COLUMNS:
                cells.append(str(row[col]) if col == "名称" else f"{int(row[col])}")
            else:
                cells.append(_cell(int(row[col]), int(row["可计算"])))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _load_hits(start: str, end: str) -> pd.DataFrame:
    frames = []
    for strategy in schemas.HIT_STRATEGIES:
        df = db.read_df(
            f"SELECT ts_code, trade_date FROM `{schemas.hit_table_name(strategy)}` "
            "WHERE trade_date>=%s AND trade_date<=%s",
            (start, end),
        )
        if df.empty:
            continue
        df["strategy"] = strategy
        frames.append(df)
    if not frames:
        raise SystemExit("区间内没有任何命中记录")
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-01-01", help="命中起始日期")
    parser.add_argument("--end", default=None, help="结束日期，缺省最新交易日")
    parser.add_argument("--min-strategies", type=int, default=2,
                        help="共振策略数下限")
    parser.add_argument("--new-only", action="store_true",
                        help="只统计首次出现的共振：前一交易日命中策略数 ≤1")
    parser.add_argument("--output-dir", default="outputs/reports")
    args = parser.parse_args()

    start = to_yyyymmdd(args.start)
    end = to_yyyymmdd(args.end) if args.end else loader.resolve_trade_date()

    market = loader.load_market_data(start, end, warmup_days=0)
    if market.empty:
        raise SystemExit(f"{start}~{end} 区间内没有行情数据")

    hits_start = _shift_days(start, -LOOKBACK_DAYS) if args.new_only else start
    hits = _load_hits(hits_start, end)
    if args.new_only:
        events = hit_performance.co_hit_starts(
            hits, loader.open_trade_dates(), min_strategies=args.min_strategies)
        events = events[events["trade_date"] >= start].reset_index(drop=True)
    else:
        events = hit_performance.group_co_hits(
            hits, min_strategies=args.min_strategies)
    if events.empty:
        raise SystemExit(f"区间内没有 ≥{args.min_strategies} 策略共振的命中")

    forward = hit_performance.forward_returns(market, events[["ts_code", "trade_date"]])
    forward = forward.merge(events, on=["ts_code", "trade_date"], how="left")
    forward.insert(0, "combination", forward["strategies"].map("+".join))

    overall = pd.DataFrame([_summary("全部共振", forward)])
    by_size = pd.DataFrame([
        _summary(f"{n} 策略共振", forward[forward["n_strategies"] == n])
        for n in sorted(forward["n_strategies"].unique())
    ])
    by_combo = pd.DataFrame([
        _summary(combo, group)
        for combo, group in forward.groupby("combination")
    ]).sort_values(["可计算", "名称"], ascending=[False, True]).reset_index(drop=True)
    combined = forward.assign(_one=1).explode("strategies")
    by_strategy = pd.DataFrame([
        _summary(f"{name}（参与共振）", group)
        for name, group in combined.groupby("strategies")
    ]).sort_values("可计算", ascending=False).reset_index(drop=True)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{start}_{end}"
    prefix = "multi_strategy_new" if args.new_only else "multi_strategy"
    md_path = out_dir / f"{prefix}_performance_{stamp}.md"
    csv_path = out_dir / f"{prefix}_forward_returns_{stamp}.csv"
    forward.to_csv(csv_path, index=False, encoding="utf-8-sig")

    title = ("# 多策略共振（首次出现）表现报告" if args.new_only
             else "# 多策略共振表现报告")
    event_line = (
        f"- 事件定义：T 日被 ≥{args.min_strategies} 个策略共振，"
        "且前一交易日命中策略数 ≤1（首次出现）"
        if args.new_only else
        f"- 事件定义：同一交易日同一股票被 ≥{args.min_strategies} 个策略命中"
    )
    header = [
        title,
        "",
        f"- 区间：{start} ~ {end}（按命中日 T 起算）",
        event_line,
    ]
    if args.new_only:
        header.append("- 前一交易日 = 交易日历上紧邻的上一个交易日；未命中、停牌均按 0 个策略计"
                      "（命中表数据自 2024-01-02 起）")
    header += [
        "- 规则：T 日共振 → T+1 开盘价买入 → T+2 收盘价卖出（不复权价，即行情界面真实成交价；持有期内分红现金未计入）",
        "- 收益率 =（T+2 收盘 − T+1 开盘）/ T+1 开盘 × 100%；>0 记盈利，≤0 记亏",
        "- T+1/T+2 为交易日历上紧邻的两个交易日；买入日或卖出日无行情（停牌）跳过，"
        "T+2 超出数据范围记数据不足",
        "- 分桶互斥：<-5；-5~-2（含 -5、-2）；-2~0（含 0）；0~2；2~5；>5（含 5）；"
        "括号内百分比 = 占可计算次数",
        "",
    ]
    sections = [
        ("## 总览", overall),
        ("## 按共振策略数", by_size),
        ("## 按策略组合（按事件数降序）", by_combo),
        ("## 按策略参与（一个事件计入其全部成员策略，合计会大于总览）", by_strategy),
    ]
    body = []
    for section_title, frame in sections:
        body += [section_title, ""] + _table_md(frame) + [""]
    md_path.write_text("\n".join(header + body), encoding="utf-8")
    print(f"报告：{md_path}")
    print(f"明细：{csv_path}")


if __name__ == "__main__":
    main()
