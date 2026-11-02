"""用 Tushare 分钟行情重算 daily.high / daily.low。

核心口径：
- 只使用 09:30:00 ~ 15:00:00 的分钟 bar；
- 每个交易日的 high 取分钟 high 的最大值，low 取分钟 low 的最小值；
- 默认把日线 open/close 也纳入 high/low 边界，避免出现
  high < open/close 或 low > open/close 的非法 OHLC；
- 不修改 open/close/vol/amount 等其他字段。

典型用法：
    uv run python tmp_scripts/rebuild_daily_high_low.py --dry-run --codes 002208.SZ
    uv run python tmp_scripts/rebuild_daily_high_low.py --freq 60min --yes
    uv run python tmp_scripts/rebuild_daily_high_low.py --freq 1min --start 2024-01-01 --yes

注意：stk_mins 的调用次数远高于 daily。默认 60min 频度下，
每只股票约需 1~2 次调用；1min 频度下，每 30 个交易日约需 1 次调用。
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import time
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import baostock as bs
import numpy as np
import pandas as pd
import pymysql

from quant.data import db
from quant.data.tushare_client import TushareClient

SESSION_START = dt_time(9, 30)
SESSION_END = dt_time(15, 0)
MAX_MINUTE_BARS = 7800
DEFAULT_START = "20180101"
DEFAULT_END = "latest"
DEFAULT_FREQ = "60min"
DEFAULT_RATE = 450
DEFAULT_MAX_REQUESTS = 0
DEFAULT_SOURCE = "tushare"
DEFAULT_SLEEP = 0.05
DEFAULT_TIMEOUT = 120.0
MAX_MISSING_RATIO = 0.05
BAOSTOCK_START = "20200101"
DB_RETRIES = 3
DB_RETRY_WAIT = 3.0
DEFAULT_STATE_FILE = Path("data_cache/daily_high_low_backfill_state.json")
DEFAULT_MISSING_FILE = Path("data_cache/daily_high_low_missing.csv")

FREQ_BARS_PER_DAY = {
    "1min": 240,
    "5min": 48,
    "15min": 16,
    "30min": 8,
    "60min": 4,
}

BAOSTOCK_FREQ = {
    "1min": "1",
    "5min": "5",
    "15min": "15",
    "30min": "30",
    "60min": "60",
}


class RequestLimitReached(RuntimeError):
    """达到 --max-requests 时抛出，便于安全停止。"""


def compact_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def _display_date(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def _db_retry(func, *args, **kwargs):
    """远端 RDS 偶发超时后重试，避免整批任务中断。"""
    last_exc: Exception | None = None
    for attempt in range(DB_RETRIES):
        try:
            return func(*args, **kwargs)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            last_exc = exc
            if attempt >= DB_RETRIES - 1:
                raise
            wait = DB_RETRY_WAIT * (attempt + 1)
            print(f"[DB] 连接异常，{wait:.0f} 秒后重试：{exc}")
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc


def aggregate_minute_high_low(bars: pd.DataFrame) -> pd.DataFrame:
    """把分钟 bar 聚合为每日 high/low，只保留 09:30-15:00。"""
    empty = pd.DataFrame(columns=[
        "trade_date", "minute_high", "minute_low", "minute_open",
        "minute_close", "minute_bars",
    ])
    if bars is None or bars.empty or "trade_time" not in bars.columns:
        return empty

    work = bars.copy()
    work["trade_time"] = pd.to_datetime(work["trade_time"], errors="coerce")
    work = work.dropna(subset=["trade_time"])
    if work.empty:
        return empty

    times = work["trade_time"].dt.time
    work = work[(times >= SESSION_START) & (times <= SESSION_END)].copy()
    if work.empty:
        return empty

    for col in ("open", "high", "low", "close"):
        if col not in work.columns:
            return empty
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["high", "low"]).sort_values("trade_time")
    if work.empty:
        return empty

    work["trade_date"] = work["trade_time"].dt.strftime("%Y%m%d")
    agg = work.groupby("trade_date", sort=True).agg(
        minute_high=("high", "max"),
        minute_low=("low", "min"),
        minute_open=("open", "first"),
        minute_close=("close", "last"),
        minute_bars=("high", "size"),
    )
    return agg.reset_index()


def aggregate_baostock_high_low(bars: pd.DataFrame) -> pd.DataFrame:
    """把 BaoStock 分钟 bar 聚合为每日 high/low，只保留 09:30-15:00。

    BaoStock 返回列：date,time,high,low；time 形如 20260714103000000。
    """
    empty = pd.DataFrame(columns=[
        "trade_date", "minute_high", "minute_low", "minute_bars",
    ])
    required = {"date", "time", "high", "low"}
    if bars is None or bars.empty or not required.issubset(bars.columns):
        return empty

    work = bars.copy()
    work["trade_time"] = pd.to_datetime(
        work["time"].astype(str).str[:14],
        format="%Y%m%d%H%M%S",
        errors="coerce",
    )
    work = work.dropna(subset=["trade_time"])
    if work.empty:
        return empty

    times = work["trade_time"].dt.time
    work = work[(times >= SESSION_START) & (times <= SESSION_END)].copy()
    if work.empty:
        return empty

    for col in ("high", "low"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["high", "low"])
    if work.empty:
        return empty

    work["trade_date"] = work["date"].astype(str).str.replace("-", "", regex=False)
    agg = work.groupby("trade_date", sort=True).agg(
        minute_high=("high", "max"),
        minute_low=("low", "min"),
        minute_bars=("high", "size"),
    )
    return agg.reset_index()


def merge_daily_high_low(
    daily: pd.DataFrame,
    minute_agg: pd.DataFrame,
    clamp_ohlc: bool = True,
) -> pd.DataFrame:
    """把分钟 high/low 对齐到 daily，并计算 new_high/new_low。"""
    if daily.empty or minute_agg.empty:
        return pd.DataFrame()

    work = daily.merge(minute_agg, on="trade_date", how="inner")
    if work.empty:
        return work
    for col in ("open", "close", "high", "low"):
        work[col] = pd.to_numeric(work[col], errors="coerce")

    valid = work["minute_high"].notna() & work["minute_low"].notna()
    work = work[valid].copy()
    if work.empty:
        return work

    if clamp_ohlc:
        # 开盘集合竞价和收盘价也必须落在日线 high/low 内。
        work["new_high"] = work[["minute_high", "open", "close"]].max(axis=1)
        work["new_low"] = work[["minute_low", "open", "close"]].min(axis=1)
    else:
        work["new_high"] = work["minute_high"]
        work["new_low"] = work["minute_low"]

    work["new_high"] = work["new_high"].round(4)
    work["new_low"] = work["new_low"].round(4)
    return work


def changed_high_low_rows(merged: pd.DataFrame) -> pd.DataFrame:
    """只返回 high/low 发生变化的行。"""
    if merged.empty:
        return merged
    old_high = pd.to_numeric(merged["high"], errors="coerce")
    old_low = pd.to_numeric(merged["low"], errors="coerce")
    changed = (
        ~np.isclose(merged["new_high"], old_high, atol=1e-9, equal_nan=True)
        | ~np.isclose(merged["new_low"], old_low, atol=1e-9, equal_nan=True)
    )
    return merged[changed].copy()


def date_windows(dates: list[str], window_days: int) -> list[list[str]]:
    if window_days < 1:
        raise ValueError("window_days 必须 >= 1")
    return [dates[i:i + window_days] for i in range(0, len(dates), window_days)]


def _update_daily_rows(rows: list[tuple[float, float, str, str]]) -> int:
    if not rows:
        return 0
    sql = (
        "UPDATE `daily` SET high=%s, low=%s "
        "WHERE ts_code=%s AND trade_date=%s"
    )
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            total = 0
            for i in range(0, len(rows), 1000):
                total += cur.executemany(sql, rows[i:i + 1000])
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and "runs" in value:
            return value
    except Exception:  # noqa: BLE001 - 状态文件损坏时重新开始
        pass
    return {"runs": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _run_key(start: str, end: str, freq: str, window_days: int,
             clamp_ohlc: bool, source: str = "tushare") -> str:
    return (
        f"{source}:{start}:{end}:{freq}:{window_days}"
        f":clamp={int(clamp_ohlc)}"
    )


def _auto_window_days(freq: str) -> int:
    bars_per_day = FREQ_BARS_PER_DAY[freq]
    return max(1, MAX_MINUTE_BARS // bars_per_day)


class MinuteHighLowFetcher:
    def __init__(self, client: TushareClient, freq: str,
                 max_requests: int = 0):
        self.client = client
        self.freq = freq
        self.max_requests = max_requests
        self.requests = 0

    def _call(self, code: str, dates: list[str]) -> pd.DataFrame:
        if self.max_requests > 0 and self.requests >= self.max_requests:
            raise RequestLimitReached(
                f"已达到 --max-requests={self.max_requests}，停止请求"
            )
        start = f"{_display_date(dates[0])} 09:00:00"
        end = f"{_display_date(dates[-1])} 15:30:00"
        self.requests += 1
        bars = self.client.call(
            "stk_mins", ts_code=code, freq=self.freq,
            start_date=start, end_date=end,
        )
        return bars if bars is not None else pd.DataFrame()

    def fetch(self, code: str, dates: list[str]) -> pd.DataFrame:
        """递归分片，避免单次 8000 行限制导致静默截断。"""
        if not dates:
            return pd.DataFrame()
        bars = self._call(code, dates)
        if len(bars) >= MAX_MINUTE_BARS and len(dates) > 1:
            mid = len(dates) // 2
            left = self.fetch(code, dates[:mid])
            right = self.fetch(code, dates[mid:])
            return pd.concat([left, right], ignore_index=True)
        return aggregate_minute_high_low(bars)


def to_baostock_code(ts_code: str) -> str | None:
    """把 002208.SZ 转成 sz.002208；BaoStock 不支持 BJ 股票。"""
    if "." not in ts_code:
        return None
    symbol, exchange = ts_code.split(".", 1)
    prefix = exchange.lower()
    if prefix not in ("sh", "sz"):
        return None
    return f"{prefix}.{symbol}"


class BaostockHighLowFetcher:
    """用 BaoStock 60 分钟行情取日内 high/low。"""

    def __init__(self, freq: str = "60min", max_requests: int = 0,
                 retries: int = 1, sleep_seconds: float = DEFAULT_SLEEP,
                 timeout_seconds: float = DEFAULT_TIMEOUT):
        if freq not in BAOSTOCK_FREQ:
            raise ValueError(f"BaoStock 不支持频度：{freq}")
        self.freq = BAOSTOCK_FREQ[freq]
        self.max_requests = max_requests
        self.retries = max(0, int(retries))
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.requests = 0
        self.errors = 0
        self._logged_in = False
        self._previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout_seconds)
        login = bs.login()
        if login.error_code != "0":
            socket.setdefaulttimeout(self._previous_timeout)
            raise RuntimeError(f"BaoStock 登录失败：{login.error_msg}")
        self._logged_in = True

    def close(self) -> None:
        if self._logged_in:
            bs.logout()
            self._logged_in = False
        socket.setdefaulttimeout(self._previous_timeout)

    def _relogin(self) -> None:
        try:
            if self._logged_in:
                bs.logout()
        except Exception:  # noqa: BLE001 - 旧连接可能已断开
            pass
        self._logged_in = False
        socket.setdefaulttimeout(self.timeout_seconds)
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock 重新登录失败：{login.error_msg}")
        self._logged_in = True

    def _query(self, bcode: str, start: str, end: str) -> tuple[list[list[str]], str, str]:
        if self.max_requests > 0 and self.requests >= self.max_requests:
            raise RequestLimitReached(
                f"已达到 --max-requests={self.max_requests}，停止请求"
            )
        rs = bs.query_history_k_data_plus(
            bcode,
            "date,time,high,low",
            start_date=_display_date(start),
            end_date=_display_date(end),
            frequency=self.freq,
            adjustflag="3",
        )
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        self.requests += 1
        return rows, rs.error_code, rs.error_msg

    def _fetch_dates(self, bcode: str, dates: list[str]) -> pd.DataFrame:
        if not dates:
            return aggregate_baostock_high_low(pd.DataFrame())
        rows: list[list[str]] = []
        error_code = ""
        error_msg = ""
        for attempt in range(self.retries + 1):
            try:
                rows, error_code, error_msg = self._query(
                    bcode, dates[0], dates[-1],
                )
            except RequestLimitReached:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络超时按失败处理
                rows = []
                error_code = "EXCEPTION"
                error_msg = str(exc)
            if error_code == "0":
                break
            if attempt < self.retries:
                try:
                    self._relogin()
                except Exception:  # noqa: BLE001 - 下一次请求会继续重试
                    pass
                if self.sleep_seconds:
                    time.sleep(max(0.5, self.sleep_seconds))
        truncated = (
            len(rows) >= 6000
            and bool(rows)
            and str(rows[-1][0]).replace("-", "") < dates[-1]
        )
        # 只有“返回行数超过 6000 行”才拆分；普通网络错误直接失败，
        # 否则连接断开时会被拆成上千次单日请求。
        oversize = error_code == "10001001"
        if (truncated or oversize) and len(dates) > 1:
            mid = len(dates) // 2
            left = self._fetch_dates(bcode, dates[:mid])
            right = self._fetch_dates(bcode, dates[mid:])
            combined = pd.concat([left, right], ignore_index=True)
            if "trade_date" in combined.columns:
                combined = combined.drop_duplicates(
                    subset=["trade_date"], keep="last",
                ).reset_index(drop=True)
            return combined
        if error_code != "0":
            self.errors += 1
            return aggregate_baostock_high_low(pd.DataFrame())
        if not rows:
            return aggregate_baostock_high_low(pd.DataFrame())
        bars = pd.DataFrame(rows, columns=["date", "time", "high", "low"])
        return aggregate_baostock_high_low(bars)

    def fetch(self, code: str, dates: list[str]) -> pd.DataFrame:
        bcode = to_baostock_code(code)
        if bcode is None or not dates:
            return aggregate_baostock_high_low(pd.DataFrame())
        valid_dates = [d for d in dates if d >= BAOSTOCK_START]
        if not valid_dates:
            return aggregate_baostock_high_low(pd.DataFrame())
        return self._fetch_dates(bcode, valid_dates)


def _missing_rows(daily: pd.DataFrame, minute_agg: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    covered = set(minute_agg["trade_date"].astype(str)) if not minute_agg.empty else set()
    missing = daily[~daily["trade_date"].astype(str).isin(covered)].copy()
    if "vol" in missing.columns:
        vol = pd.to_numeric(missing["vol"], errors="coerce")
        missing = missing[vol.fillna(0) > 0]
    return missing


def _select_daily(code: str, start: str, end: str) -> pd.DataFrame:
    return db.read_df(
        """
        SELECT ts_code, trade_date, open, close, high, low, vol
        FROM daily
        WHERE ts_code = %s AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date
        """,
        (code, start, end),
    )


def _select_codes(start: str, end: str, codes: list[str]) -> pd.DataFrame:
    df = db.read_df(
        """
        SELECT ts_code, COUNT(*) AS n
        FROM daily
        WHERE trade_date BETWEEN %s AND %s
        GROUP BY ts_code
        ORDER BY ts_code
        """,
        (start, end),
    )
    if codes:
        df = df[df["ts_code"].isin(codes)].copy()
    return df.reset_index(drop=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用 Tushare stk_mins 重算 daily.high / daily.low"
    )
    parser.add_argument("--source", choices=("tushare",),
                        default=DEFAULT_SOURCE,
                        help="分钟数据源，仅支持 tushare")
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"起始日期，默认 {DEFAULT_START}")
    parser.add_argument("--end", default=DEFAULT_END,
                        help=f"结束日期，默认 {DEFAULT_END}（daily 最新日期）")
    parser.add_argument("--codes", default="",
                        help="只处理指定股票，逗号分隔，例如 002208.SZ,600545.SH")
    parser.add_argument("--freq", default=DEFAULT_FREQ,
                        choices=tuple(FREQ_BARS_PER_DAY),
                        help=f"stk_mins 频度，默认 {DEFAULT_FREQ}")
    parser.add_argument("--window-days", type=int, default=0,
                        help="每次 API 调用覆盖的交易日数；0 表示按频度自动计算")
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE,
                        help=f"stk_mins 每分钟调用上限，默认 {DEFAULT_RATE}")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                        help="最多请求次数，0 表示不限制；适合小范围测试")
    parser.add_argument("--max-codes", type=int, default=0,
                        help="最多处理多少只股票，0 表示不限制")
    parser.add_argument("--max-failed", type=int, default=100,
                        help="失败股票数达到该值时停止，默认 100；0 表示不限制")
    parser.add_argument("--retries", type=int, default=1,
                        help="单次 API 失败重试次数，默认 1")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                        help=f"BaoStock 每次请求后的等待秒数，默认 {DEFAULT_SLEEP}")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"BaoStock 网络超时秒数，默认 {DEFAULT_TIMEOUT:g}")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="分片编号，从 0 开始；用于多进程分批更新")
    parser.add_argument("--shards", type=int, default=1,
                        help="分片总数，默认 1（不拆分）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只计算并打印差异，不更新数据库")
    parser.add_argument("--yes", action="store_true",
                        help="跳过更新前确认")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE),
                        help=f"断点文件，默认 {DEFAULT_STATE_FILE}")
    parser.add_argument("--missing-file", default=str(DEFAULT_MISSING_FILE),
                        help=f"缺失分钟数据的样本输出，默认 {DEFAULT_MISSING_FILE}")
    parser.add_argument("--no-resume", action="store_true",
                        help="忽略已有断点，从头开始")
    parser.add_argument("--reset-state", action="store_true",
                        help="删除断点文件后重新开始")
    parser.add_argument("--no-clamp", dest="clamp_ohlc", action="store_false",
                        help="不把 open/close 纳入 high/low 边界")
    parser.set_defaults(clamp_ohlc=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = Path(args.state_file)
    missing_path = Path(args.missing_file)

    max_daily = _db_retry(db.scalar, "SELECT MAX(trade_date) FROM daily")
    if not max_daily:
        print("daily 表为空，无法重建 high/low")
        return 2
    end = str(max_daily) if str(args.end).lower() in ("", "latest", "none") else compact_date(args.end)
    start = compact_date(args.start)
    if start > end:
        print(f"参数错误：起始日期 {start} 晚于结束日期 {end}")
        return 2
    if args.rate < 1:
        print("参数错误：--rate 必须 >= 1")
        return 2
    if args.window_days < 0:
        print("参数错误：--window-days 不能为负数")
        return 2
    if args.shards < 1:
        print("参数错误：--shards 必须 >= 1")
        return 2
    if not 0 <= args.shard_index < args.shards:
        print("参数错误：--shard-index 必须在 [0, --shards) 内")
        return 2
    if args.sleep < 0:
        print("参数错误：--sleep 不能为负数")
        return 2
    if args.timeout <= 0:
        print("参数错误：--timeout 必须 > 0")
        return 2
    if args.max_failed < 0:
        print("参数错误：--max-failed 不能为负数")
        return 2

    codes = [x.strip() for x in args.codes.split(",") if x.strip()]
    code_df = _db_retry(_select_codes, start, end, codes)
    if code_df.empty:
        print("没有需要处理的股票")
        return 2

    skipped_bj = 0
    if args.source == "baostock":
        bj_mask = code_df["ts_code"].str.upper().str.endswith(".BJ")
        skipped_bj = int(bj_mask.sum())
        code_df = code_df[~bj_mask].reset_index(drop=True)
    if args.shards > 1:
        code_df = code_df.iloc[args.shard_index::args.shards].reset_index(drop=True)
    if code_df.empty:
        print("当前分片没有需要处理的股票")
        return 0

    if args.source == "baostock":
        window_days = 0
        estimate_calls = len(code_df)
    else:
        window_days = args.window_days or _auto_window_days(args.freq)
        estimate_calls = int(sum(
            math.ceil(int(n) / window_days) for n in code_df["n"]
        ))
    total_rows = int(code_df["n"].sum())
    print("=" * 90)
    print(f"daily high/low 重建（{args.source}）")
    print(f"  日期区间：{start} ~ {end}")
    print(f"  股票数：{len(code_df):,}，daily 行数：{total_rows:,}")
    if args.shards > 1:
        print(f"  分片：{args.shard_index + 1}/{args.shards}")
    if skipped_bj:
        print(f"  跳过 BJ 股票：{skipped_bj:,} 只（BaoStock 不支持）")
    print(f"  分钟频度：{args.freq}")
    if args.source == "baostock":
        print(f"  BaoStock 分钟数据起点：{BAOSTOCK_START}")
        print(f"  预计 API 请求：约 {estimate_calls:,} 次（超 6000 行时自动拆分）")
        print(f"  每请求间隔：{args.sleep:g} 秒")
    else:
        print(f"  每批交易日数：{window_days}")
        print(f"  预计 API 调用：约 {estimate_calls:,} 次"
              f"（未计入大响应递归拆分）")
        print(f"  当前限频：{args.rate} 次/分钟")
        if estimate_calls:
            print(f"  理论最短耗时：{estimate_calls / args.rate / 60:.1f} 小时")
    print(f"  状态文件：{state_path}")
    print(f"  缺失样本：{missing_path}")
    print("-" * 90)

    if args.reset_state and state_path.exists():
        state_path.unlink()
    state = {"runs": {}} if args.no_resume else _load_state(state_path)
    run_key = _run_key(
        start, end, args.freq, window_days, args.clamp_ohlc, args.source,
    )
    run_state = state.setdefault("runs", {}).setdefault(run_key, {
        "completed": {},
        "failed": {},
        "stats": {
            "codes": 0, "rows": 0, "computed": 0,
            "changed": 0, "missing": 0, "requests": 0,
        },
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    completed: dict[str, Any] = run_state.setdefault("completed", {})
    failed: dict[str, Any] = run_state.setdefault("failed", {})
    stats: dict[str, int] = run_state.setdefault("stats", {
        "codes": 0, "rows": 0, "computed": 0,
        "changed": 0, "missing": 0, "requests": 0,
    })

    if not args.dry_run and not args.yes:
        print("即将更新 daily 表的 high/low 字段（只改这两列）。")
        answer = input("确认继续请输入 YES：")
        if answer.strip() != "YES":
            print("已取消，未做任何修改。")
            return 0

    if args.source == "baostock":
        fetcher: Any = BaostockHighLowFetcher(
            freq=args.freq,
            max_requests=args.max_requests,
            retries=args.retries,
            sleep_seconds=args.sleep,
            timeout_seconds=args.timeout,
        )
    else:
        client = TushareClient(
            calls_per_minute=args.rate,
            api_rate_limits={"stk_mins": args.rate},
            max_retries=max(1, args.retries),
        )
        fetcher = MinuteHighLowFetcher(client, args.freq, args.max_requests)
    missing_records: list[dict[str, Any]] = []
    processed = 0
    consecutive_failures = 0
    started = time.monotonic()

    try:
        for row in code_df.itertuples(index=False):
            code = str(row.ts_code)
            if code in completed:
                continue
            if args.max_codes > 0 and processed >= args.max_codes:
                print(f"已达到 --max-codes={args.max_codes}，停止。")
                break
            processed += 1
            daily = _db_retry(_select_daily, code, start, end)
            if daily.empty:
                continue
            daily["trade_date"] = daily["trade_date"].astype(str)
            dates = daily["trade_date"].tolist()
            print(f"[{processed}/{len(code_df)}] {code} "
                  f"{dates[0]}~{dates[-1]}（{len(dates)} 个交易日）")
            minute_agg = fetcher.fetch(code, dates)
            merged = merge_daily_high_low(daily, minute_agg, args.clamp_ohlc)
            changed = changed_high_low_rows(merged)
            missing = _missing_rows(daily, minute_agg)
            if args.source == "baostock" and not missing.empty:
                missing = missing[
                    missing["trade_date"].astype(str) >= BAOSTOCK_START
                ].copy()

            updated = 0
            if not args.dry_run and not changed.empty:
                rows = [
                    (float(r.new_high), float(r.new_low),
                     str(r.ts_code), str(r.trade_date))
                    for r in changed.itertuples(index=False)
                ]
                updated = _db_retry(_update_daily_rows, rows)

            record = {
                "rows": len(daily),
                "computed": len(merged),
                "changed": len(changed),
                "updated": updated,
                "missing_dates": missing["trade_date"].astype(str).tolist(),
            }
            eligible_rows = len(daily)
            if args.source == "baostock":
                eligible_rows = int((
                    daily["trade_date"].astype(str) >= BAOSTOCK_START
                ).sum())
            missing_ratio = (
                len(missing) / eligible_rows if eligible_rows else 0.0
            )
            if missing.empty or missing_ratio <= MAX_MISSING_RATIO:
                completed[code] = record
                failed.pop(code, None)
                consecutive_failures = 0
            else:
                failed[code] = record
                consecutive_failures += 1
            stats["codes"] += 1
            if (
                args.max_failed > 0
                and consecutive_failures >= args.max_failed
            ):
                print(
                    f"连续失败股票数达到 --max-failed={args.max_failed}，"
                    "停止以避免继续产生缺失样本。"
                )
                break
            stats["rows"] += len(daily)
            stats["computed"] += len(merged)
            stats["changed"] += len(changed)
            stats["missing"] += len(missing)
            stats["requests"] = fetcher.requests
            run_state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if not args.dry_run:
                _save_state(state_path, state)

            for rec in missing.itertuples(index=False):
                missing_records.append({
                    "ts_code": rec.ts_code,
                    "trade_date": rec.trade_date,
                    "open": rec.open,
                    "close": rec.close,
                    "high": rec.high,
                    "low": rec.low,
                    "vol": rec.vol,
                })
            print(f"  分钟覆盖 {len(merged)} / {len(daily)}，"
                  f"high/low 变化 {len(changed)}，写入 {updated}，"
                  f"缺失 {len(missing)}")
    except RequestLimitReached as exc:
        print(str(exc))
    except Exception as exc:  # noqa: BLE001 - 保留断点并给出明确错误
        print(f"处理中断：{type(exc).__name__}: {exc}")
        if not args.dry_run:
            _save_state(state_path, state)
        return 1
    finally:
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()

    if not args.dry_run:
        _save_state(state_path, state)
        if missing_records:
            missing_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(missing_records).drop_duplicates(
                subset=["ts_code", "trade_date"]
            ).to_csv(missing_path, index=False, encoding="utf-8-sig")

    elapsed = time.monotonic() - started
    print("")
    print(f"完成/中断：已处理 {stats['codes']:,} 只股票，"
          f"分钟覆盖 {stats['computed']:,} 行，"
          f"high/low 变化 {stats['changed']:,} 行，"
          f"缺失 {stats['missing']:,} 行，"
          f"失败待重试 {len(failed):,} 只，"
          f"请求 {fetcher.requests:,} 次，耗时 {elapsed:.1f} 秒")
    if args.dry_run:
        print("dry-run：未写入数据库，也未保存断点")
    else:
        print(f"断点文件：{state_path}")
        if missing_records:
            print(f"缺失样本：{missing_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
