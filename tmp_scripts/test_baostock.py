import baostock as bs
import pandas as pd
from pathlib import Path


# =========================
# 参数配置
# =========================

STOCK_CODE = "sz.000716"  # 合肥城建
TRADE_DATE = "2026-11-02"

OUTPUT_DIR = Path("data/minute")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"


def main():

    print("=" * 60)
    print("60分钟历史行情查询")
    print(f"股票代码：{STOCK_CODE}")
    print(f"交易日期：{TRADE_DATE}")
    print("=" * 60)

    # 1. 登录 BaoStock
    lg = bs.login()

    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败：{lg.error_msg}")

    try:
        # 2. 获取当天60分钟K线
        rs = bs.query_history_k_data_plus(
            STOCK_CODE,
            FIELDS,
            start_date=TRADE_DATE,
            end_date=TRADE_DATE,
            frequency="60",
            adjustflag="3",  # 不复权
        )

        if rs.error_code != "0":
            raise RuntimeError(f"行情查询失败：{rs.error_msg}")

        # 3. 读取数据
        rows = []

        while rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            print("没有获取到行情数据，请检查交易日期和数据源。")
            return

        df = pd.DataFrame(rows, columns=rs.fields)

        # 4. 数据类型转换
        price_cols = ["open", "high", "low", "close", "volume", "amount"]

        for col in price_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # BaoStock 时间格式：
        # YYYYMMDDHHMMSSsss
        df["bar_time"] = pd.to_datetime(
            df["time"].str[:14], format="%Y%m%d%H%M%S", errors="coerce"
        )

        df = df.dropna(subset=["bar_time", "high", "low"])

        # 5. 筛选交易时段
        minutes = df["bar_time"].dt.hour * 60 + df["bar_time"].dt.minute


        morning = minutes.between(9 * 60 + 35, 11 * 60 + 30)

        afternoon = minutes.between(13 * 60 + 5, 15 * 60)

        df = df[morning | afternoon].copy()

        df = df.sort_values("bar_time")

        if df.empty:
            print("指定交易时段没有有效的5分钟行情。")
            return

        # 6. 计算最高价和最低价
        high_idx = df["high"].idxmax()
        low_idx = df["low"].idxmin()

        high_row = df.loc[high_idx]
        low_row = df.loc[low_idx]

        # 7. 输出完整行情
        # print("\n【5分钟K线数据】")

        # print(
        #     df[
        #         [
        #             "bar_time",
        #             "open",
        #             "high",
        #             "low",
        #             "close",
        #             "volume",
        #             "amount",
        #         ]
        #     ].to_string(index=False)
        # )

        # 8. 输出统计结果
        # print("\n" + "=" * 60)
        # print("【日内最高价 / 最低价统计】")
        # print("=" * 60)

        # print(f"股票：合肥城建 ({STOCK_CODE})")

        # print(f"日期：{TRADE_DATE}")

        # print(f"5分钟K线数量：{len(df)}")

        print(f"日内最高价：{high_row['high']:.4f} 元")

        # print(f"最高价所在K线：{high_row['bar_time']}")

        print(f"日内最低价：{low_row['low']:.4f} 元")

        # print(f"最低价所在K线：{low_row['bar_time']}")



    finally:
        # 10. 退出
        bs.logout()


if __name__ == "__main__":
    main()
