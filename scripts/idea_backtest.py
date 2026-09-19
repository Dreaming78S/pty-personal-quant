"""通用命中回测：推荐日 R 盘后按条件筛选命中个股，R+1 开盘价买入、R+1+N 收盘价卖出。

用法示例：
    uv run python scripts/idea_backtest.py --start 2024-01-01 \
        --t1-strategies ma_volume,rps_breakout --t1-min-count 2 --hold 1 \
        --t2-strategies turtle_trade --cum-min-count 2 --mv-min 0 --mv-max 50000
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from quant.data import db, schemas
from quant.engine import hit_performance, idea_backtest, loader
from quant.utils.dates import to_yyyymmdd

LOOKBACK_DAYS = 40
BUCKETS = hit_performance.BUCKET_NAMES


def _parse_strategies(text: str | None) -> tuple[str, ...]:
    names = tuple(s.strip() for s in (text or "").split(",") if s.strip())
    unknown = [n for n in names if n not in schemas.HIT_STRATEGIES]
    if unknown:
        raise SystemExit(f"未知策略：{'、'.join(unknown)}；"
                         f"可选：{'、'.join(schemas.HIT_STRATEGIES)}")
    return names


def _shift_days(yyyymmdd: str, days: int) -> str:
    return (datetime.strptime(yyyymmdd, "%Y%m%d")
            + timedelta(days=days)).strftime("%Y%m%d")


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
        raise SystemExit(f"{start}~{end} 区间内没有任何命中记录")
    return pd.concat(frames, ignore_index=True)


def _run_signature(*, hold: int, t1_min_count: int,
                   t1_strategies: tuple[str, ...], t2_min_count: int | None,
                   t2_strategies: tuple[str, ...], cum_min_count: int,
                   cum_strategies: tuple[str, ...],
                   mv_min: float, mv_max: float) -> str:
    parts = [f"n{hold}"]
    t1 = f"t1ge{t1_min_count}"
    if t1_strategies:
        t1 += "-" + "+".join(t1_strategies)
    parts.append(t1)
    if t2_strategies or t2_min_count is not None:
        t2 = "t2ge" + (str(t2_min_count) if t2_min_count is not None else "any")
        if t2_strategies:
            t2 += "-" + "+".join(t2_strategies)
        parts.append(t2)
    cum = f"cumge{cum_min_count}"
    if cum_strategies:
        cum += "-" + "+".join(cum_strategies)
    parts.append(cum)
    if (mv_min, mv_max) != (0.0, 50000.0):
        parts.append(f"mv{mv_min:g}-{mv_max:g}")
    return "_".join(parts)


def _describe(count: int | None, strategies: tuple[str, ...]) -> str:
    parts = []
    if strategies:
        parts.append("必须同时命中 " + "、".join(strategies))
    if count is not None:
        parts.append(f"至少 {count} 个不同策略")
    return "；".join(parts) if parts else "不限"


def _cell(count: int, total: int) -> str:
    pct = f"{count / total * 100:.2f}%" if total else "-"
    return f"{count} ({pct})"


def _summary_row(label: str, forward: pd.DataFrame) -> dict:
    ok = forward.loc[forward["status"] == "ok", "ret_pct"]
    stats = hit_performance.bucket_stats(ok)
    return {
        "名称": label,
        "事件数": len(forward),
        "可计算": stats["可计算"],
        "停牌跳过": int((forward["status"] == "suspended").sum()),
        "数据不足": int((forward["status"] == "no_data").sum()),
        "盈利": stats["盈利"],
        **{b: stats[b] for b in BUCKETS},
        "平均收益": f"{ok.mean():+.2f}%" if len(ok) else "-",
        "中位收益": f"{ok.median():+.2f}%" if len(ok) else "-",
    }


def _table_md(frame: pd.DataFrame) -> list[str]:
    lines = ["| " + " | ".join(frame.columns) + " |",
             "|" + "---|" * len(frame.columns)]
    for _, row in frame.iterrows():
        cells = []
        for col in frame.columns:
            if col in ("名称", "平均收益", "中位收益"):
                cells.append(str(row[col]))
            elif col in ("事件数", "可计算", "停牌跳过", "数据不足"):
                cells.append(f"{int(row[col])}")
            else:
                cells.append(_cell(int(row[col]), int(row["可计算"])))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-01",
                        help="推荐日起始（含），默认 2024-01-01")
    parser.add_argument("--end", default=None, help="推荐日结束（含），缺省最新交易日")
    parser.add_argument("--hold", type=int, default=1,
                        help="持有交易日数 N：R+1 开盘买入、R+1+N 收盘卖出，默认 1")
    parser.add_argument("--t1-strategies", default="",
                        help="推荐日 R 必须同时命中的策略（逗号分隔，默认无）")
    parser.add_argument("--t1-min-count", type=int, default=1,
                        help="推荐日 R 不同策略数下限，默认 1")
    parser.add_argument("--t2-strategies", default="",
                        help="前一交易日 R-1 必须同时命中的策略（默认无）")
    parser.add_argument("--t2-min-count", type=int, default=None,
                        help="前一交易日 R-1 不同策略数下限，缺省不限")
    parser.add_argument("--cum-strategies", default="",
                        help="R/R-1/R-2 累计必须命中的策略（默认无）")
    parser.add_argument("--cum-min-count", type=int, default=1,
                        help="R/R-1/R-2 累计不同策略数下限，默认 1")
    parser.add_argument("--mv-min", type=float, default=0.0,
                        help="推荐日总市值下限（亿），默认 0")
    parser.add_argument("--mv-max", type=float, default=50000.0,
                        help="推荐日总市值上限（亿），默认 50000")
    parser.add_argument("--tag", default="", help="报告文件名标签，缺省用参数指纹")
    parser.add_argument("--output-dir", default="outputs/reports")
    args = parser.parse_args()

    if args.hold < 0:
        raise SystemExit("--hold 不能为负")
    if args.mv_max < args.mv_min:
        raise SystemExit("--mv-max 不能小于 --mv-min")
    t1_strategies = _parse_strategies(args.t1_strategies)
    t2_strategies = _parse_strategies(args.t2_strategies)
    cum_strategies = _parse_strategies(args.cum_strategies)

    start = to_yyyymmdd(args.start)
    end = to_yyyymmdd(args.end) if args.end else loader.resolve_trade_date()
    if end < start:
        raise SystemExit(f"结束日期 {end} 早于起始日期 {start}")

    market = loader.load_market_data(start, end, warmup_days=0)
    if market.empty:
        raise SystemExit(f"{start}~{end} 区间内没有行情数据")

    hits = _load_hits(_shift_days(start, -LOOKBACK_DAYS), end)
    entries = idea_backtest.screen_ideas(
        hits, loader.open_trade_dates(), start, end,
        t1_strategies=t1_strategies, t1_min_count=args.t1_min_count,
        t2_strategies=t2_strategies, t2_min_count=args.t2_min_count,
        cum_strategies=cum_strategies, cum_min_count=args.cum_min_count)
    screened = len(entries)
    entries = idea_backtest.filter_by_market_cap(entries, market,
                                                 args.mv_min, args.mv_max)
    if entries.empty:
        raise SystemExit(f"筛选后没有符合条件的推荐"
                         f"（命中筛选 {screened} 条，市值过滤后 0 条）")

    forward = hit_performance.forward_returns(market, entries,
                                              sell_offset=1 + args.hold)
    forward = forward.merge(entries, on=["ts_code", "trade_date"], how="left")
    forward["strategies"] = forward["strategies"].map("+".join)
    forward = forward[["ts_code", "trade_date", "buy_date", "sell_date",
                       "n_t1", "n_t2", "n_cum", "strategies", "total_mv_yi",
                       "buy_open", "sell_close", "ret_pct", "status"]]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{start}_{end}"
    signature = args.tag or _run_signature(
        hold=args.hold, t1_min_count=args.t1_min_count,
        t1_strategies=t1_strategies, t2_min_count=args.t2_min_count,
        t2_strategies=t2_strategies, cum_min_count=args.cum_min_count,
        cum_strategies=cum_strategies, mv_min=args.mv_min, mv_max=args.mv_max)
    md_path = out_dir / f"idea_backtest_{stamp}_{signature}.md"
    csv_path = out_dir / f"idea_backtest_trades_{stamp}_{signature}.csv"
    forward.to_csv(csv_path, index=False, encoding="utf-8-sig")

    summary = pd.DataFrame([_summary_row("全部推荐", forward)])
    header = [
        "# 命中回测报告",
        "",
        f"- 区间：{start} ~ {end}（推荐日 R 盘后筛选，R 不早于起始日）",
        f"- 交易：R 的下一交易日 T 以开盘价买入 → T+{args.hold} 收盘价卖出"
        f"（N={args.hold}；不复权价，即行情界面真实成交价；持有期内分红现金未计入）",
        "- 收益率 =（卖出日收盘 − 买入日开盘）/ 买入日开盘 × 100%；>0 记盈利，≤0 记亏",
        f"- 推荐日 R 条件：{_describe(args.t1_min_count, t1_strategies)}",
        f"- 前一交易日 R-1 条件：{_describe(args.t2_min_count, t2_strategies)}",
        f"- 累计 R/R-1/R-2 条件：{_describe(args.cum_min_count, cum_strategies)}",
        f"- 总市值（R 日 total_mv）：{args.mv_min:g} 亿 ~ {args.mv_max:g} 亿"
        "（闭区间，缺市值数据剔除）",
        "- 买入日/卖出日为交易日历上紧邻的交易日；无行情（停牌）跳过，"
        "超出数据范围记数据不足",
        "- 分桶互斥：<-5；-5~-2（含 -5、-2）；-2~0（含 0）；0~2；2~5；>5（含 5）；"
        "括号内百分比 = 占可计算次数",
        f"- 筛选过程：命中候选 {screened} 条 → 市值过滤后 {len(entries)} 条",
        "",
        "## 总览",
        "",
    ]
    body = _table_md(summary) + [""]
    md_path.write_text("\n".join(header + body), encoding="utf-8")
    print(f"报告：{md_path}")
    print(f"明细：{csv_path}")


if __name__ == "__main__":
    main()
