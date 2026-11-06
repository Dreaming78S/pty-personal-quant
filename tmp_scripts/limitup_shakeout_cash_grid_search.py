"""对 limit_up_shakeout 现金收益做 止盈分档 × T+1 买入低开区间 × 限价买入深度 的网格搜索。

搜索维度：
- 止盈分档 EXIT_TIERS_GRID：T+1 收盘对买入价涨幅 → T+2 止盈率的分段函数（左开右闭），
  33 个候选，覆盖 6 个设计维度：
  * 固定阈值锚点（单段）：0% / 0.5% / 1% / 1.5% / 2% / 3% / 4% / 5%，用于对比
    “动态分档是否优于固定阈值”并定位固定阈值的最优水平；半步档（0.5% / 1.5%）
    同时服务于对角线对比（见下）；
  * 高段边界：>3% / >5% / >7% / >10% 单独分档，或不分高段；
  * 高段阈值：3% / 4% / 5%；
  * 低段目标：0% / 0.5% / 1% / 1.5%，或 T+2 开盘即走；
  * 中段细分：0/3/7、0/5/7，以及整体平移 -0.5pp / +0.5pp / +1pp（分档20/19/25）；
  * 止损设计：-2% / -3% / -5% 止损率 × -3% / -5% / -7% 触发边界，以及
    “深跌段要求小反弹 +0.5%”的温和变体；分档22 为旧版系统默认的 5 档结构；
    分档23 / 分档24 为综合前三轮发现的两条“低段 0.5% + 高段 4%”假设。
- T+1 买入低开区间 ENTRY_GAP_GRID：[-5%, 0%] 与 [-11%, 0%] 两个候选。
- 限价买入深度 DELTA_GRID（百分点）：0 / 0.5 / 1.0 / 1.5 / 2.0；0 即开盘价买入
  （与历史轮次完全同口径）。买入基准价 = (1 + a - delta%) × close(T)，
  a = open(T+1)/close(T)-1（不复权）；T+1 最低价触及基准价才成交（按基准价成交），
  未触及则该样本不交易。分档涨幅与止盈目标价均以买入基准价为基准。

delta 与止盈阈值存在近似平移等价：目标价换回开盘价口径 ≈ 阈值 r − delta。因此
网格内置“对角线对比”：取 r = delta（固定0%@d0 → 固定0.5%@d0.5 → 固定1%@d1 →
固定1.5%@d1.5 → 固定2%@d2；3 档阶梯 分档2@d0 → 分档19@d0.5 → 分档25@d1），
卖出目标相对开盘价几乎不变，唯一变化是买入更便宜 + 成交率下降，用于隔离
“限价折价本身是否划算”；固定阈值单段方案的分档涨幅口径不影响其行为，对角线最纯净。

其余筛选参数固定（见 FIXED 参数区）：统计区间 20181101 ~ latest、T 日高开
[0%, 11%]、T 日收盘涨跌幅 [0%, 11%]、近 5 日涨幅 [-1000%, 1000%]、要求信号日
最低价不破前一日收盘（后复权）。

收益口径与 limitup_shakeout_cash_returns.py 完全一致（直接复用其函数）：
- 按每笔预算取最大整数手（floor）；实际买入价 = 买入基准价 × (1 + 滑点)；
- 卖出分支按当前分档参数逐样本重新判定：T+2 开盘高于目标价（买入价×(1+阈值)）
  按开盘卖、盘中最高价触及目标价按目标价卖、否则 T+2 收盘卖；
- 收盘跌停顺延下一交易日开盘，最多 10 个交易日，超期按最后收盘估值；
- 佣金 0.025%（最低 5 元）、印花税 0.05%（卖出）、过户费 0.001%（双边）、滑点 0。

样本冻结设计：每个 (低开区间 × delta) 只用“分档1”跑一次样本筛选（delta>0 时
样本集合依赖限价成交判定，必须逐 delta 冻结），该 (低开 × delta) 下全部 33 个
分档共用同一样本集合（按构造一致），保证对比只反映离场规则差异；运行时另用两个
极端阈值轮廓（固定0%、固定5%）独立重筛校验各冻结集合差异（见报告）。

用法：
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py --no-report
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py --slippage 0.001

all_trades.csv 默认只写 Top50 组合 ∪ 基准扫描组合（DELTA_ANCHOR_SCHEMES × 低开1
× 全部 delta，见 DEFAULT_TRADES_TOP_N），避免百万行级文件；全部组合的指标都在
grid_results.csv。除现金明细外还带 5 个分析列（按 命中日+代码 附着）：
行业（命中表快照）、买入日低开(%)（open(T+1)/除权后昨收-1，低开 3.3 个点记 -3.3）、
买入日流通市值(万元)（T+1 日 daily_basic.circ_mv）、量能比(T/T-1)（T 日成交量/
T-1 日成交量，与策略“放量”同口径）、涨停日K线实体(%)（(T-1 收盘-T-1 开盘)/
T-1 开盘×100）。

默认生成报告到 outputs/limitup_shakeout_cash_grid_search_<时间戳>/。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TMP_DIR))
os.chdir(ROOT)

from quant.data import db  # noqa: E402
from quant.engine.exit_tiers import (  # noqa: E402
    format_tiers_summary,
    tier_labels,
    tiers_from_scalar,
    tiers_to_jsonable,
    validate_exit_tiers,
)

import limitup_shakeout_cash_returns as cash  # noqa: E402
import limitup_shakeout_filtered_returns as filtered  # noqa: E402

# ==============================================================================
# 固定筛选参数（与 limitup_shakeout_cash_returns.py 的默认口径一致）
DEFAULT_START = "20181101"
DEFAULT_END = "latest"
DEFAULT_GAP = (0.00, 0.11)
DEFAULT_CLOSE_CHG = (0.0, 0.11)
DEFAULT_RISE_DAYS = 5
DEFAULT_RISE = (-10, 10)
DEFAULT_REQUIRE_LOW_SUPPORT = True

FAMILY_FIXED = "固定阈值"
FAMILY_POSITIVE = "全正阈值"
FAMILY_NEGATIVE = "含负阈值"
FAMILY_ORDER = (FAMILY_FIXED, FAMILY_POSITIVE, FAMILY_NEGATIVE)

# 止盈分档候选：(名称, 分段函数, 族)。区间左开右闭，None 表示 ±∞。
# 分档1~4 与上一轮网格搜索定义完全一致，便于纵向对比。
EXIT_TIERS_GRID: list[tuple[str, tuple, str]] = [
    # ---- 固定阈值锚点（单段，不分档）；半步档 0.5%/1.5% 服务于对角线对比（r=delta）----
    ("固定0%", tiers_from_scalar(0.00), FAMILY_FIXED),
    ("固定0.5%", tiers_from_scalar(0.005), FAMILY_FIXED),
    ("固定1%", tiers_from_scalar(0.01), FAMILY_FIXED),
    ("固定1.5%", tiers_from_scalar(0.015), FAMILY_FIXED),
    ("固定2%", tiers_from_scalar(0.02), FAMILY_FIXED),
    ("固定3%", tiers_from_scalar(0.03), FAMILY_FIXED),
    ("固定4%", tiers_from_scalar(0.04), FAMILY_FIXED),
    ("固定5%", tiers_from_scalar(0.05), FAMILY_FIXED),
    # ---- 全正阈值（无止损）----
    ("分档1", (
        (None, 0.00, 0.01),
        (0.00, None, 0.02),
    ), FAMILY_POSITIVE),   # 2 档基线
    ("分档2", (
        (None, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_POSITIVE),   # 3 档基线（上一轮最优）
    ("分档5", (
        (None, 0.00, 0.01),
        (0.00, 0.05, 0.02),
        (0.05, None, 0.03),
    ), FAMILY_POSITIVE),   # 高段边界 5%
    ("分档6", (
        (None, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.04),
    ), FAMILY_POSITIVE),   # 高段阈值 4%
    ("分档7", (
        (None, 0.00, 0.01),
        (0.00, 0.03, 0.02),
        (0.03, None, 0.03),
    ), FAMILY_POSITIVE),   # 高段边界 3%
    ("分档8", (
        (None, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, 0.10, 0.03),
        (0.10, None, 0.05),
    ), FAMILY_POSITIVE),   # 超高段 >10% → 5%
    ("分档9", (
        (None, 0.00, 0.01),
        (0.00, 0.03, 0.015),
        (0.03, 0.07, 0.025),
        (0.07, None, 0.035),
    ), FAMILY_POSITIVE),   # 中段细分 0/3/7
    ("分档10", (
        (None, 0.00, 0.01),
        (0.00, 0.05, 0.015),
        (0.05, 0.07, 0.025),
        (0.07, None, 0.035),
    ), FAMILY_POSITIVE),   # 中段细分 0/5/7
    ("分档11", (
        (None, 0.00, 0.005),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_POSITIVE),   # 低段目标 0.5%
    ("分档12", (
        (None, 0.00, 0.015),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_POSITIVE),   # 低段目标 1.5%
    ("分档13", (
        (None, 0.00, 0.00),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_POSITIVE),   # 低段保本 0%
    ("分档16", (
        (None, -0.05, 0.005),
        (-0.05, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_POSITIVE),   # 深跌段要求小反弹 +0.5%（不认赔）
    ("分档19", (
        (None, 0.00, 0.015),
        (0.00, 0.07, 0.025),
        (0.07, None, 0.035),
    ), FAMILY_POSITIVE),   # 整体 +0.5pp
    ("分档20", (
        (None, 0.00, 0.005),
        (0.00, 0.07, 0.015),
        (0.07, None, 0.025),
    ), FAMILY_POSITIVE),   # 整体 -0.5pp
    ("分档25", (
        (None, 0.00, 0.02),
        (0.00, 0.07, 0.03),
        (0.07, None, 0.04),
    ), FAMILY_POSITIVE),   # 整体 +1pp（对角线：分档2@d0 ≈ 分档19@d0.5 ≈ 分档25@d1 的开盘价口径目标）
    ("分档23", (
        (None, 0.00, 0.005),
        (0.00, 0.07, 0.03),
        (0.07, None, 0.04),
    ), FAMILY_POSITIVE),   # 综合：低段 0.5% + 中段 3% + 高段 4%
    ("分档24", (
        (None, 0.00, 0.005),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.04),
    ), FAMILY_POSITIVE),   # 综合：低段 0.5% + 中段 2% + 高段 4%
    # ---- 含负阈值（含止损或低段负目标）----
    ("分档3", (
        (None, -0.05, -0.03),
        (-0.05, 0.00, 0.01),
        (0.00, None, 0.02),
    ), FAMILY_NEGATIVE),   # 3 档含止损（上一轮）
    ("分档4", (
        (None, -0.05, -0.03),
        (-0.05, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_NEGATIVE),   # 4 档含止损（上一轮）
    ("分档14", (
        (None, -0.05, -0.02),
        (-0.05, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_NEGATIVE),   # 止损 -2%@-5%
    ("分档15", (
        (None, -0.05, -0.05),
        (-0.05, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_NEGATIVE),   # 止损 -5%@-5%（深止损）
    ("分档17", (
        (None, -0.03, -0.02),
        (-0.03, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_NEGATIVE),   # 止损 -2%@-3%（更早触发）
    ("分档18", (
        (None, -0.07, -0.03),
        (-0.07, 0.00, 0.01),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_NEGATIVE),   # 止损 -3%@-7%（更晚触发）
    ("分档21", (
        (None, 0.00, -0.10),
        (0.00, 0.07, 0.02),
        (0.07, None, 0.03),
    ), FAMILY_NEGATIVE),   # 低段 T+2 开盘即走（-10% 目标近似无条件开盘卖）
    ("分档22", (
        (None, -0.05, -0.02),
        (-0.05, 0.00, 0.01),
        (0.00, 0.05, 0.02),
        (0.05, 0.07, 0.03),
        (0.07, None, 0.04),
    ), FAMILY_NEGATIVE),   # 旧版默认 5 档（-2%@-5% + 0/5/7 细分）
]

# 样本冻结的基准分档：该 (低开 × delta) 下所有分档共用它筛出的样本集合
REFERENCE_SCHEME = "分档1"
# 校验冻结样本集合用的极端阈值轮廓：最低（固定0%）与最高（固定5%，全段 5% 为最高阈值）
VERIFY_SCHEMES = ("固定0%", "固定5%")

# T+1 买入低开区间候选：(名称, 区间)
ENTRY_GAP_GRID = [
    ("低开1", (-0.05, 0.0)),
    ("低开2", (-0.11, 0.0)),
]

# 限价买入深度候选（百分点）：0 = 开盘价买入（原口径，与历史轮次直接可比）；
# 名称用于组合 id（分档×低开×dX）。买入基准价 = (1 + a - delta%) × close(T)，
# a = open(T+1)/close(T)-1（不复权），T+1 最低价触及基准价才成交
DELTA_GRID: list[tuple[str, float]] = [
    ("d0", 0.0),
    ("d0.5", 0.5),
    ("d1", 1.0),
    ("d1.5", 1.5),
    ("d2", 2.0),
]

# delta 主扫描的基准分档：报告的 delta 结论行引用这些分档，且其 (× 低开1 × 全部 delta)
# 组合无条件写入 all_trades.csv 明细
DELTA_ANCHOR_SCHEMES = (
    "固定0%", "固定1%", "固定2%", "固定3%", "固定5%",
    "分档1", "分档2", "分档4", "分档22", "分档23", "分档24",
)

# 关键单变量对比（基准组合, 变体组合, 标题）；组合 id = 分档名×低开名×delta名。
# 第一组为分档间/低开间对比，固定在 delta=0（与历史轮次同口径）；
# 第二、三组为 delta 相关对比：对角线（卖出目标相对开盘价 ≈ 不变，只加深折价）
# 与同分档加深折价（阈值相对买入成本不变）。
COMPARISONS: list[tuple[str, str, str]] = [
    # ---- 分档对比（×低开1×d0，历史口径连续）----
    ("分档2×低开1×d0", "分档4×低开1×d0", "加止损 -3%@-5%"),
    ("分档2×低开1×d0", "分档14×低开1×d0", "加止损 -2%@-5%"),
    ("分档2×低开1×d0", "分档15×低开1×d0", "加止损 -5%@-5%"),
    ("分档2×低开1×d0", "分档17×低开1×d0", "止损更早触发（-2%@-3%）"),
    ("分档2×低开1×d0", "分档18×低开1×d0", "止损更晚触发（-3%@-7%）"),
    ("分档2×低开1×d0", "分档16×低开1×d0", "深跌段改为要求 +0.5% 小反弹"),
    ("分档2×低开1×d0", "分档22×低开1×d0", "5 档结构（止损 -2%@-5% + 0/5/7 中段细分）"),
    ("分档1×低开1×d0", "分档2×低开1×d0", ">7% 单独分档 → +3%"),
    ("分档1×低开1×d0", "分档5×低开1×d0", ">5% 单独分档 → +3%"),
    ("分档1×低开1×d0", "分档7×低开1×d0", ">3% 单独分档 → +3%"),
    ("分档2×低开1×d0", "分档6×低开1×d0", ">7% 段阈值 3% → 4%"),
    ("分档2×低开1×d0", "分档8×低开1×d0", ">10% 再单独分档 → +5%"),
    ("分档2×低开1×d0", "分档9×低开1×d0", "中段细分 0/3/7"),
    ("分档2×低开1×d0", "分档10×低开1×d0", "中段细分 0/5/7"),
    ("分档2×低开1×d0", "分档11×低开1×d0", "低段目标 1% → 0.5%"),
    ("分档2×低开1×d0", "分档12×低开1×d0", "低段目标 1% → 1.5%"),
    ("分档2×低开1×d0", "分档13×低开1×d0", "低段目标 1% → 0%（保本）"),
    ("分档2×低开1×d0", "分档21×低开1×d0", "低段改为 T+2 开盘即走"),
    ("分档2×低开1×d0", "分档19×低开1×d0", "整体阈值 +0.5pp"),
    ("分档2×低开1×d0", "分档20×低开1×d0", "整体阈值 -0.5pp"),
    ("分档2×低开1×d0", "固定0%×低开1×d0", "换成固定保本 0%"),
    ("分档2×低开1×d0", "固定1%×低开1×d0", "换成固定 1%"),
    ("分档2×低开1×d0", "固定2%×低开1×d0", "换成固定 2%"),
    ("分档2×低开1×d0", "固定3%×低开1×d0", "换成固定 3%"),
    ("固定3%×低开1×d0", "固定4%×低开1×d0", "固定阈值 3% → 4%"),
    ("固定4%×低开1×d0", "固定5%×低开1×d0", "固定阈值 4% → 5%"),
    ("分档11×低开1×d0", "分档23×低开1×d0", "中段 2% → 3%（低段 0.5%、高段 4% 保持）"),
    ("分档11×低开1×d0", "分档24×低开1×d0", "高段 3% → 4%（低段 0.5%、中段 2% 保持）"),
    ("分档2×低开1×d0", "分档2×低开2×d0", "低开放宽到 [-11%, 0%]"),
    ("分档14×低开1×d0", "分档14×低开2×d0", "低开放宽（止损 -2%@-5% 下）"),
    ("分档22×低开1×d0", "分档22×低开2×d0", "低开放宽（默认 5 档下）"),
    ("固定2%×低开1×d0", "固定2%×低开2×d0", "低开放宽（固定 2% 下）"),
    # ---- 对角线：卖出目标相对开盘价 ≈ 不变，只加深买入折价（r = delta）----
    ("固定0%×低开1×d0", "固定0.5%×低开1×d0.5", "对角线：折价 0.5%（目标 ≈ 开盘价不变）"),
    ("固定0.5%×低开1×d0.5", "固定1%×低开1×d1", "对角线：折价 1.0%"),
    ("固定1%×低开1×d1", "固定1.5%×低开1×d1.5", "对角线：折价 1.5%"),
    ("固定1.5%×低开1×d1.5", "固定2%×低开1×d2", "对角线：折价 2.0%"),
    ("分档2×低开1×d0", "分档19×低开1×d0.5", "对角线（3 档）：+0.5pp 阈值配 0.5% 折价"),
    ("分档19×低开1×d0.5", "分档25×低开1×d1", "对角线（3 档）：+1pp 阈值配 1% 折价"),
    # ---- 同分档加深折价（阈值相对买入成本不变）----
    ("分档2×低开1×d0", "分档2×低开1×d2", "分档2：折价 0 → 2%"),
    ("固定2%×低开1×d0", "固定2%×低开1×d2", "固定2%：折价 0 → 2%"),
    ("分档4×低开1×d0", "分档4×低开1×d2", "含止损分档4：折价 0 → 2%"),
    ("分档22×低开1×d0", "分档22×低开1×d2", "旧默认 5 档：折价 0 → 2%"),
]
# ==============================================================================

DEFAULT_POSITION = 100_000.0
DEFAULT_LOT_MODE = "floor"
DEFAULT_MAX_DELAY_DAYS = 10
DEFAULT_COMMISSION = 0.00025
DEFAULT_MIN_COMMISSION = 5.0
DEFAULT_STAMP = 0.0005
DEFAULT_TRANSFER = 0.00001
DEFAULT_SLIPPAGE = 0.0
DEFAULT_GENERATE_REPORT = True
# all_trades.csv 写入的组合数上限（按平均净收益率取 Top-N，0 = 全部组合）；
# 另外无条件包含 DELTA_ANCHOR_SCHEMES × 低开1 × 全部 delta 的基准扫描组合
DEFAULT_TRADES_TOP_N = 50


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="limit_up_shakeout 现金收益网格搜索：止盈分档 × T+1 买入低开区间"
    )
    parser.add_argument(
        "--start", default=DEFAULT_START, help=f"命中日起始，默认 {DEFAULT_START}"
    )
    parser.add_argument(
        "--end", default=DEFAULT_END,
        help=f"命中日结束，默认 {DEFAULT_END}，即命中表最新日期",
    )
    parser.add_argument(
        "--position", type=float, default=DEFAULT_POSITION,
        help=f"每个样本投入资金上限，默认 {DEFAULT_POSITION:,.0f}",
    )
    parser.add_argument(
        "--lot-mode", choices=("nearest", "floor"), default=DEFAULT_LOT_MODE,
        help="手数取整：nearest=最接近预算，floor=不超过预算",
    )
    parser.add_argument(
        "--max-delay-days", type=int, default=DEFAULT_MAX_DELAY_DAYS,
        help=f"尾盘跌停后最多顺延的交易日数，默认 {DEFAULT_MAX_DELAY_DAYS}",
    )
    parser.add_argument(
        "--commission", type=float, default=DEFAULT_COMMISSION,
        help=f"佣金率，默认 {DEFAULT_COMMISSION}",
    )
    parser.add_argument(
        "--min-commission", type=float, default=DEFAULT_MIN_COMMISSION,
        help=f"最低佣金，默认 {DEFAULT_MIN_COMMISSION:,.0f} 元",
    )
    parser.add_argument(
        "--stamp", type=float, default=DEFAULT_STAMP,
        help=f"印花税率（卖出），默认 {DEFAULT_STAMP}",
    )
    parser.add_argument(
        "--transfer", type=float, default=DEFAULT_TRANSFER,
        help=f"过户费率（双边），默认 {DEFAULT_TRANSFER}",
    )
    parser.add_argument(
        "--slippage", type=float, default=DEFAULT_SLIPPAGE,
        help=f"单边滑点，默认 {DEFAULT_SLIPPAGE}",
    )
    parser.add_argument(
        "--report", dest="generate_report", action="store_true",
        help="生成 report.md / CSV（默认开启）",
    )
    parser.add_argument(
        "--no-report", dest="generate_report", action="store_false",
        help="只打印控制台，不写报告文件",
    )
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument(
        "--output-dir", default="outputs", help="报告输出根目录，默认 outputs"
    )
    return parser.parse_args(argv)


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def fmt_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.2f}"


def fmt_num(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.2f}"


def fmt_range(value: tuple[float, float]) -> str:
    return f"[{fmt_pct(value[0], signed=False)}, {fmt_pct(value[1], signed=False)}]"


def md_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_无_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)


def with_retry(fn, desc: str, attempts: int = 3, wait: float = 8.0):
    """带重试的调用包装：本地到 RDS 的链路偶发超时，避免长跑中途报废。"""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络/连接类错误统一重试
            if i == attempts:
                raise
            print(f"  {desc} 第 {i}/{attempts} 次尝试失败：{exc}；{wait:g}s 后重试 ...")
            time.sleep(wait)


def preload_delay_cache(
    frozen_inputs: dict[tuple[str, str], pd.DataFrame],
    calendar: list[str],
    max_delay_days: int,
) -> dict:
    """预取所有可能触发跌停顺延的 (代码, 日期) 行情行，供 calc_trades 注入。

    候选 = 卖出日（T+2）收盘跌停的样本；该标志来自价格本身、与分档和 delta
    均无关，是任何组合下顺延查询的完全超集。把候选后续 max_delay_days 个交易日
    的行情一次性批量拉齐，数据库里不存在的键也记 None，保证组合循环阶段
    零数据库访问（不受网络抖动影响）。
    """
    date_pos = {d: i for i, d in enumerate(calendar)}
    wanted: set[tuple[str, str]] = set()
    n_candidates = 0
    for cash_input in frozen_inputs.values():
        candidates = cash_input.loc[
            cash_input["卖出日是否跌停"].astype(bool), ["股票代码", "次次日日期"]
        ]
        n_candidates += len(candidates)
        for code, t2_date in zip(candidates["股票代码"], candidates["次次日日期"]):
            pos = date_pos.get(str(t2_date))
            if pos is None:
                continue
            for date in calendar[pos + 1 : pos + 1 + max_delay_days]:
                wanted.add((str(code), date))
    cache: dict = {}
    if not wanted:
        print("预取跌停顺延行情：无候选样本")
        return cache
    codes = sorted({c for c, _d in wanted})
    dates = sorted({d for _c, d in wanted})
    print(
        f"预取跌停顺延行情：{n_candidates} 个候选样本，"
        f"{len(wanted):,} 个 (代码, 日期) ..."
    )
    for i in range(0, len(codes), 500):
        chunk = codes[i : i + 500]
        code_ph = ",".join(["%s"] * len(chunk))
        date_ph = ",".join(["%s"] * len(dates))
        frame = with_retry(
            lambda: db.read_df(
                "SELECT d.ts_code, d.trade_date, d.open, d.high, d.close, "
                "d.pre_close, a.adj_factor, l.down_limit "
                "FROM daily d "
                "LEFT JOIN adj_factor a ON a.ts_code = d.ts_code "
                "  AND a.trade_date = d.trade_date "
                "LEFT JOIN stk_limit l ON l.ts_code = d.ts_code "
                "  AND l.trade_date = d.trade_date "
                f"WHERE d.ts_code IN ({code_ph}) AND d.trade_date IN ({date_ph})",
                (*chunk, *dates),
            ),
            "预取顺延行情",
        )
        if frame.empty:
            continue
        for row in frame.to_dict("records"):
            cache[(str(row["ts_code"]), str(row["trade_date"]))] = {
                "ts_code": row["ts_code"],
                "trade_date": row["trade_date"],
                **{
                    col: pd.to_numeric(row.get(col), errors="coerce")
                    for col in ("open", "high", "close", "pre_close",
                                "adj_factor", "down_limit")
                },
            }
    for key in wanted:
        cache.setdefault(key, None)  # 数据库无此行也标记，避免循环阶段回查
    hit = sum(1 for v in cache.values() if v is not None)
    print(f"预取完成：数据库命中 {hit:,} / 需要 {len(wanted):,}")
    return cache


def fetch_by_code_dates(
    table: str,
    columns: list[str],
    codes: list[str],
    dates: list[str],
) -> pd.DataFrame:
    """按 (代码集合 × 日期集合) 分块批量查询 daily / daily_basic 的指定列。"""
    if not codes or not dates:
        return pd.DataFrame(columns=["ts_code", "trade_date", *columns])
    frames = []
    date_ph = ",".join(["%s"] * len(dates))
    select_cols = ", ".join(["ts_code", "trade_date", *columns])
    for i in range(0, len(codes), 500):
        chunk = codes[i : i + 500]
        code_ph = ",".join(["%s"] * len(chunk))
        frames.append(
            with_retry(
                lambda: db.read_df(
                    f"SELECT {select_cols} FROM {table} "
                    f"WHERE ts_code IN ({code_ph}) AND trade_date IN ({date_ph})",
                    (*chunk, *dates),
                ),
                f"读取 {table} 分析列",
            )
        )
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["ts_code", "trade_date", *columns])
    out["ts_code"] = out["ts_code"].astype(str)
    out["trade_date"] = out["trade_date"].astype(str)
    for col in columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def build_enrichment(
    base_samples: pd.DataFrame,
    prices: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    """为逐笔明细构建分析列，按 (命中日, 代码) 键返回：

    - 买入日低开(%)：`open(T+1) / 除权后昨收 - 1` 的百分数（低开 3.3 个点记 -3.3，
      与样本筛选的低开条件同口径）；
    - 行业：命中表 `hit_limit_up_shakeout.industry` 快照；
    - 买入日流通市值(万元)：T+1 日 `daily_basic.circ_mv`；
    - 量能比(T/T-1)：T 日成交量 / T-1 日成交量（与策略“放量”条件同口径，单位手、
      比值无量纲）；
    - 涨停日K线实体(%)：`(T-1 收盘 - T-1 开盘) / T-1 开盘 × 100`，T-1 即涨停日。
    """
    codes = sorted(base_samples["ts_code"].dropna().astype(str).unique())
    hit_dates = sorted(set(base_samples["hit_date"].dropna().astype(str)))

    # 涨停日 K 线实体：T-1 开盘/收盘（不复权），从已加载行情取
    prev_px = prices[["ts_code", "trade_date", "open", "close"]].rename(
        columns={"trade_date": "prev_date", "open": "prev_open", "close": "prev_close"}
    )
    body = base_samples[["ts_code", "prev_date"]].merge(
        prev_px, on=["ts_code", "prev_date"], how="left"
    )
    body_pct = (body["prev_close"] - body["prev_open"]) / body["prev_open"] * 100.0

    # 量能比：T 与 T-1 的成交量
    prev_dates = sorted(set(base_samples["prev_date"].dropna().astype(str)))
    vol_df = fetch_by_code_dates(
        "daily", ["vol"], codes, sorted(set(hit_dates) | set(prev_dates))
    )
    vol_t = base_samples[["ts_code", "hit_date"]].merge(
        vol_df.rename(columns={"trade_date": "hit_date", "vol": "vol_t"}),
        on=["ts_code", "hit_date"], how="left",
    )["vol_t"]
    vol_prev = base_samples[["ts_code", "prev_date"]].merge(
        vol_df.rename(columns={"trade_date": "prev_date", "vol": "vol_prev"}),
        on=["ts_code", "prev_date"], how="left",
    )["vol_prev"]
    vol_ratio = (vol_t / vol_prev).replace([np.inf, -np.inf], np.nan)

    # T+1 流通市值（万元）
    t1_dates = sorted(set(base_samples["t1_date"].dropna().astype(str)))
    mv_df = fetch_by_code_dates("daily_basic", ["circ_mv"], codes, t1_dates)
    circ_mv = base_samples[["ts_code", "t1_date"]].merge(
        mv_df.rename(columns={"trade_date": "t1_date"}),
        on=["ts_code", "t1_date"], how="left",
    )["circ_mv"]

    # 行业快照（取命中表自身的 industry，保留命中当时的分类）
    industry_df = with_retry(
        lambda: db.read_df(
            "SELECT trade_date, ts_code, industry FROM hit_limit_up_shakeout "
            "WHERE trade_date BETWEEN %s AND %s",
            (start, end),
        ),
        "读取行业快照",
    )
    industry_df = industry_df.rename(columns={"trade_date": "hit_date"})
    industry_df["hit_date"] = industry_df["hit_date"].astype(str)
    industry_df["ts_code"] = industry_df["ts_code"].astype(str)

    enrich = pd.DataFrame(
        {
            "hit_date": base_samples["hit_date"].astype(str).to_numpy(),
            "ts_code": base_samples["ts_code"].astype(str).to_numpy(),
            "买入日低开(%)": (
                pd.to_numeric(base_samples["entry_gap"], errors="coerce") * 100.0
            ).to_numpy(),
            "涨停日K线实体(%)": body_pct.to_numpy(),
            "量能比(T/T-1)": vol_ratio.to_numpy(),
            "买入日流通市值(万元)": circ_mv.to_numpy(),
        }
    )
    enrich = enrich.merge(
        industry_df[["hit_date", "ts_code", "industry"]],
        on=["hit_date", "ts_code"], how="left",
    ).rename(columns={"industry": "行业"})
    return enrich


def combo_metrics(
    combo_id: str,
    tier_name: str,
    family: str,
    tiers: tuple,
    entry_name: str,
    entry_gap: tuple[float, float],
    delta: float,
    delta_name: str,
    trades: pd.DataFrame,
    args: argparse.Namespace,
) -> dict:
    realized = trades[trades["exit_status"] == "已卖出"]
    buy_fail = int(trades["exit_status"].isin(cash.BUY_FAIL_STATUSES).sum())
    stats_pnl = cash.stats(realized["net_pnl"])
    stats_ret = cash.stats(realized["net_return"])
    total_pnl = float(realized["net_pnl"].sum()) if len(realized) else 0.0
    return {
        "组合": combo_id,
        "分档": tier_name,
        "族": family,
        "tiers": tiers,
        "低开": entry_name,
        "entry_gap": entry_gap,
        "delta": delta,
        "delta名": delta_name,
        "样本数": len(trades),
        "实际买入": int((trades["shares"] > 0).sum()),
        "买入失败": buy_fail,
        "已卖出": len(realized),
        "延期卖出": int((trades["delay_days"] > 0).sum()),
        "超期估值": int(
            trades["exit_status"].astype(str).str.startswith("超期未卖出").sum()
        ),
        "未卖出": len(trades) - len(realized) - buy_fail,
        "净盈亏合计": total_pnl,
        "平均净盈亏": stats_pnl.get("平均"),
        "平均净收益率": stats_ret.get("平均"),
        "中位数": stats_ret.get("中位数"),
        "胜率": stats_pnl.get("胜率"),
        "盈亏比": stats_pnl.get("盈亏比"),
        "最小收益率": stats_ret.get("最小"),
        "最大收益率": stats_ret.get("最大"),
        "收益/预算": (
            total_pnl / (len(realized) * args.position) if len(realized) else None
        ),
    }


def yearly_rows(trades: pd.DataFrame) -> list[dict]:
    realized = trades[trades["exit_status"] == "已卖出"].copy()
    if realized.empty:
        return []
    rows = []
    for year, group in realized.groupby(realized["hit_date"].str[:4], sort=True):
        rows.append(
            {
                "年份": year,
                "已卖出": len(group),
                "净盈亏合计": float(group["net_pnl"].sum()),
                "平均净收益率": float(group["net_return"].mean()),
                "胜率": float((group["net_pnl"] > 0).mean()),
            }
        )
    return rows


def tier_breakdown_rows(trades: pd.DataFrame, tiers: tuple) -> list[dict]:
    rate_map = dict(zip(tier_labels(tiers), (t[2] for t in tiers)))
    rows = []
    for label in tier_labels(tiers):
        group = trades[trades["exit_tier"] == label]
        if group.empty:
            continue
        realized = group[group["exit_status"] == "已卖出"]
        rows.append(
            {
                "止盈档位": label,
                "止盈阈值": rate_map[label],
                "样本数": len(group),
                "已卖出": len(realized),
                "平均净收益率": (
                    float(realized["net_return"].mean()) if len(realized) else None
                ),
                "胜率": (
                    float((realized["net_pnl"] > 0).mean()) if len(realized) else None
                ),
                "净盈亏合计": float(realized["net_pnl"].sum()) if len(realized) else 0.0,
            }
        )
    return rows


def sell_mode_rows(trades: pd.DataFrame) -> list[dict]:
    rows = []
    valid = trades[trades["exit_mode"].astype(str).ne("")]
    total = len(trades) or 1
    for mode, group in valid.groupby("exit_mode", sort=True):
        realized = group[group["exit_status"] == "已卖出"]
        rows.append(
            {
                "卖出方式": str(mode),
                "样本数": len(group),
                "占比": len(group) / total,
                "平均净收益率": (
                    float(realized["net_return"].mean()) if len(realized) else None
                ),
                "净盈亏合计": float(realized["net_pnl"].sum()) if len(realized) else 0.0,
            }
        )
    return rows


def build_report(
    args: argparse.Namespace,
    start: str,
    end: str,
    results: list[dict],
    yearly: dict[str, list[dict]],
    tier_breakdown: dict[str, list[dict]],
    sell_mode: dict[str, list[dict]],
    frozen_stats: dict[tuple[str, str], dict],
    verify_diffs: dict[str, int],
    generated_at: str,
) -> str:
    TOP_SHOWN = 30  # md 报告只展示 Top 组合，全量见 grid_results.csv
    delta_first, delta_last = DELTA_GRID[0][0], DELTA_GRID[-1][0]
    entry_first = ENTRY_GAP_GRID[0][0]
    years = sorted({row["年份"] for rows in yearly.values() for row in rows})
    by_avg = sorted(
        results, key=lambda r: (r["平均净收益率"] is None, r["平均净收益率"] or 0.0),
        reverse=True,
    )
    index = {r["组合"]: r for r in results}
    top = by_avg[:TOP_SHOWN]

    overview_rows = []
    for r in top:
        overview_rows.append(
            {
                "组合": r["组合"],
                "族": r["族"],
                "止盈分档": format_tiers_summary(r["tiers"]),
                "T+1低开": fmt_range(r["entry_gap"]),
                "delta": f"{r['delta']:g}%",
                "样本数": r["样本数"],
                "净盈亏合计": fmt_money(r["净盈亏合计"]),
                "平均净收益率": fmt_pct(r["平均净收益率"]),
                "中位数": fmt_pct(r["中位数"]),
                "胜率": fmt_pct(r["胜率"], signed=False),
                "盈亏比": fmt_num(r["盈亏比"]),
                "收益/预算": fmt_pct(r["收益/预算"]),
                "延期": r["延期卖出"],
                "超期估值": r["超期估值"],
            }
        )

    def year_cell(rows: list[dict], year: str) -> str:
        for row in rows:
            if row["年份"] == year:
                return f"{fmt_pct(row['平均净收益率'])} ({row['已卖出']})"
        return "-"

    matrix_rows = []
    for r in top:
        rows = yearly[r["组合"]]
        positive_years = sum(1 for row in rows if row["净盈亏合计"] > 0)
        matrix_rows.append(
            {
                "组合": r["组合"],
                **{year: year_cell(rows, year) for year in years},
                "正收益年数": f"{positive_years}/{len(years)}",
            }
        )

    tier_long_rows = []
    for r in by_avg[:10]:
        for row in tier_breakdown[r["组合"]]:
            tier_long_rows.append(
                {
                    "组合": r["组合"],
                    "止盈档位": row["止盈档位"],
                    "止盈阈值": fmt_pct(row["止盈阈值"]),
                    "样本数": row["样本数"],
                    "已卖出": row["已卖出"],
                    "平均净收益率": fmt_pct(row["平均净收益率"]),
                    "胜率": fmt_pct(row["胜率"], signed=False),
                    "净盈亏合计": fmt_money(row["净盈亏合计"]),
                }
            )

    # 冻结样本与限价成交率（按 低开 × delta；留存率相对 delta=0）
    freeze_rows = []
    for entry_name, _rng in ENTRY_GAP_GRID:
        base_n = (frozen_stats.get((entry_name, delta_first)) or {}).get("samples")
        for delta_name, delta in DELTA_GRID:
            st = frozen_stats.get((entry_name, delta_name))
            if st is None:
                continue
            retention = st["samples"] / base_n if base_n else None
            freeze_rows.append(
                {
                    "低开": entry_name,
                    "delta": f"{delta:g}%",
                    "冻结样本数": st["samples"],
                    "限价未成交跳过": st["unfilled"],
                    "留存率(对delta=0)": fmt_pct(retention, signed=False),
                }
            )

    # 分档 × delta 矩阵（低开1，平均净收益率）；最优delta 与 最深-最浅 敏感度
    scheme_matrix_rows = []
    for name, _tiers, family in EXIT_TIERS_GRID:
        row = {"分档": name, "族": family}
        vals: dict[str, float | None] = {}
        for delta_name, _delta in DELTA_GRID:
            r = index.get(f"{name}×{entry_first}×{delta_name}")
            value = r["平均净收益率"] if r else None
            vals[delta_name] = value
            row[delta_name] = fmt_pct(value)
        valid = {k: v for k, v in vals.items() if v is not None}
        if valid:
            best_dn = max(valid, key=valid.get)
            row["最优delta"] = f"{best_dn}（{fmt_pct(valid[best_dn])}）"
        else:
            row["最优delta"] = "-"
        first_v, last_v = vals.get(delta_first), vals.get(delta_last)
        row[f"{delta_last}-{delta_first}"] = (
            fmt_pct(last_v - first_v)
            if first_v is not None and last_v is not None
            else "-"
        )
        scheme_matrix_rows.append(row)

    # delta 汇总：每个 (低开 × delta) 的族平均、对 delta=0 的主效应与最优组合
    delta_rows = []
    delta_effect: dict[tuple[str, str], float | None] = {}
    for entry_name, _rng in ENTRY_GAP_GRID:
        base_avg = {
            r["分档"]: r["平均净收益率"]
            for r in results
            if r["低开"] == entry_name and r["delta名"] == delta_first
        }
        for delta_name, delta in DELTA_GRID:
            subs = [
                r for r in results
                if r["低开"] == entry_name and r["delta名"] == delta_name
            ]
            if not subs:
                continue
            avgs = [r["平均净收益率"] or 0.0 for r in subs]
            best = max(subs, key=lambda r: r["平均净收益率"] or 0.0)
            diffs = [
                r["平均净收益率"] - base_avg[r["分档"]]
                for r in subs
                if r["分档"] in base_avg
                and r["平均净收益率"] is not None
                and base_avg[r["分档"]] is not None
            ]
            effect = float(np.mean(diffs)) if diffs else None
            delta_effect[(entry_name, delta_name)] = effect
            row = {
                "低开": entry_name,
                "delta": f"{delta:g}%",
                "组合数": len(subs),
                "全档平均": fmt_pct(float(np.mean(avgs))),
                "主效应(对delta=0)": fmt_pct(effect),
                "最优组合": f"{best['组合']}（{fmt_pct(best['平均净收益率'])}）",
            }
            for family in FAMILY_ORDER:
                fam_subs = [r for r in subs if r["族"] == family]
                if fam_subs:
                    row[f"{family}族平均"] = fmt_pct(
                        float(np.mean([r["平均净收益率"] or 0.0 for r in fam_subs]))
                    )
            delta_rows.append(row)

    mode_sections = []
    for r in by_avg[:5]:
        rows = sell_mode[r["组合"]]
        if not rows:
            continue
        table_rows = [
            {
                "卖出方式": row["卖出方式"],
                "样本数": row["样本数"],
                "占比": fmt_pct(row["占比"], signed=False),
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "净盈亏合计": fmt_money(row["净盈亏合计"]),
            }
            for row in rows
        ]
        mode_sections.append(
            [
                f"### {r['组合']}",
                "",
                md_table(table_rows, ["卖出方式", "样本数", "占比", "平均净收益率", "净盈亏合计"]),
                "",
            ]
        )

    comparison_lines = []
    for base_id, variant_id, title in COMPARISONS:
        base, var = index.get(base_id), index.get(variant_id)
        if not base or not var:
            continue
        comparison_lines.append(
            f"- **{title}**：{variant_id} 相对 {base_id}"
            f"（平均净收益率 {fmt_pct(base['平均净收益率'])} → {fmt_pct(var['平均净收益率'])}，"
            f"胜率 {fmt_pct(base['胜率'], signed=False)} → {fmt_pct(var['胜率'], signed=False)}，"
            f"净盈亏合计 {fmt_money(base['净盈亏合计'])} → {fmt_money(var['净盈亏合计'])} 元）"
        )

    best_avg = by_avg[0]
    best_total = max(results, key=lambda r: r["净盈亏合计"])
    max_positive = max(
        sum(1 for row in yearly[r["组合"]] if row["净盈亏合计"] > 0) for r in results
    )
    most_stable = [
        r for r in results
        if sum(1 for row in yearly[r["组合"]] if row["净盈亏合计"] > 0) == max_positive
    ]
    most_stable.sort(key=lambda r: r["平均净收益率"] or 0.0, reverse=True)

    dynamic = [r for r in results if r["族"] != FAMILY_FIXED]
    fixed = [r for r in results if r["族"] == FAMILY_FIXED]
    best_dyn = max(dynamic, key=lambda r: r["平均净收益率"] or 0.0)
    best_fix = max(fixed, key=lambda r: r["平均净收益率"] or 0.0)
    dyn_minus_fix = (best_dyn["平均净收益率"] or 0.0) - (best_fix["平均净收益率"] or 0.0)

    pos_on_e1 = [
        r for r in results
        if r["族"] == FAMILY_POSITIVE
        and r["低开"] == entry_first
        and r["delta名"] == delta_first
    ]
    neg_on_e1 = [
        r for r in results
        if r["族"] == FAMILY_NEGATIVE
        and r["低开"] == entry_first
        and r["delta名"] == delta_first
    ]
    family_delta = (
        float(np.mean([r["平均净收益率"] or 0.0 for r in neg_on_e1]))
        - float(np.mean([r["平均净收益率"] or 0.0 for r in pos_on_e1]))
    )

    entry_deltas = []
    for name, _tiers, _family in EXIT_TIERS_GRID:
        for delta_name, _delta in DELTA_GRID:
            r1 = index.get(f"{name}×低开1×{delta_name}")
            r2 = index.get(f"{name}×低开2×{delta_name}")
            if (
                r1 and r2
                and r1["平均净收益率"] is not None
                and r2["平均净收益率"] is not None
            ):
                entry_deltas.append(r2["平均净收益率"] - r1["平均净收益率"])

    # 各 delta 下的全局最优组合（跨两个低开区间）
    best_per_delta = []
    for delta_name, delta in DELTA_GRID:
        subs = [r for r in results if r["delta名"] == delta_name]
        if not subs:
            continue
        best = max(subs, key=lambda r: r["平均净收益率"] or 0.0)
        best_per_delta.append(
            f"delta {delta:g}%：**{best['组合']}**（{fmt_pct(best['平均净收益率'])}，"
            f"胜率 {fmt_pct(best['胜率'], signed=False)}）"
        )

    # 对角线（r = delta，卖出目标相对开盘价 ≈ 不变，只加深折价）
    def combo_avg_text(combo_id: str) -> str:
        r = index.get(combo_id)
        return fmt_pct(r["平均净收益率"]) if r else "-"

    diagonal_fixed = " → ".join(
        f"{delta:g}%：{combo_avg_text(f'固定{delta:g}%×{entry_first}×{delta_name}')}"
        for delta_name, delta in DELTA_GRID
    )
    diagonal_ladder = " → ".join(
        f"{scheme}@{delta_name}："
        f"{combo_avg_text(f'{scheme}×{entry_first}×{delta_name}')}"
        for scheme, delta_name in (
            ("分档2", delta_first),
            ("分档19", "d0.5"),
            ("分档25", "d1"),
        )
    )

    tier_grid_legend = [
        {"名称": name, "族": family, "分段函数": format_tiers_summary(tiers)}
        for name, tiers, family in EXIT_TIERS_GRID
    ]
    entry_grid_legend = [
        {"低开": name, "区间": fmt_range(rng)} for name, rng in ENTRY_GAP_GRID
    ]
    delta_grid_legend = [
        {
            "delta": f"{delta:g}%",
            "含义": (
                "开盘价买入（原口径）"
                if delta == 0
                else f"开盘下方 {delta:g}% 限价（买入价 = (1+a-{delta:g}%)×close(T)）"
            ),
        }
        for _name, delta in DELTA_GRID
    ]
    if verify_diffs and all(v == 0 for v in verify_diffs.values()):
        verify_text = f"全部 {len(verify_diffs)} 项均为 0 条"
    else:
        verify_text = (
            "；".join(f"{k}：{v} 条" for k, v in verify_diffs.items()) or "-"
        )

    lines = [
        "# limit_up_shakeout 现金收益网格搜索（止盈分档 × T+1 买入低开 × 限价深度）",
        "",
        f"- 生成时间：{generated_at}",
        f"- 命中表：`hit_limit_up_shakeout`，区间 `{start}` ~ `{end}`",
        f"- T 日高开区间：`{fmt_range(DEFAULT_GAP)}`（闭区间）",
        f"- T 日收盘涨跌幅区间：`{fmt_range(DEFAULT_CLOSE_CHG)}`（闭区间）",
        f"- 近 {DEFAULT_RISE_DAYS} 日涨幅区间：`{fmt_range(DEFAULT_RISE)}`（闭区间，等于不限）",
        f"- 信号日最低价条件：{'要求' if DEFAULT_REQUIRE_LOW_SUPPORT else '不要求'}"
        "（后复权 `low(T) >= close(T-1)`）",
        f"- 每笔预算：`{args.position:,.0f}` 元；手数模式：`{args.lot_mode}`（100 股/手）",
        f"- 佣金 `{args.commission:.5%}`（最低 {args.min_commission:.0f} 元）、"
        f"印花税 `{args.stamp:.3%}`（卖出）、过户费 `{args.transfer:.4%}`（双边）、"
        f"滑点 `{args.slippage:.2%}`（单边）",
        f"- 尾盘跌停顺延：最多 {args.max_delay_days} 个交易日，超期按最后收盘估值",
        "",
        "## 网格定义",
        "",
        md_table(tier_grid_legend, ["名称", "族", "分段函数"]),
        "",
        md_table(entry_grid_legend, ["低开", "区间"]),
        "",
        md_table(delta_grid_legend, ["delta", "含义"]),
        "",
        f"> 共 {len(EXIT_TIERS_GRID)} 个分档 × {len(ENTRY_GAP_GRID)} 个低开区间 × "
        f"{len(DELTA_GRID)} 个 delta = {len(results)} 个组合。同一 (低开 × delta) 下"
        f"所有分档共用 `{REFERENCE_SCHEME}` 筛出的同一样本集合（按构造一致），对比只"
        "反映离场规则差异；冻结样本校验（极端阈值轮廓独立重筛的差异）："
        f"{verify_text}。各分档 JSON 定义见 `run_meta.json`。",
        "",
        "## 冻结样本与限价成交率",
        "",
        md_table(
            freeze_rows,
            ["低开", "delta", "冻结样本数", "限价未成交跳过", "留存率(对delta=0)"],
        ),
        "",
        "> delta 越深，T+1 最低价触及买入价的样本越少（留存率下降）；被跳过的样本"
        "不参与该 delta 下的任何组合。各组合指标中的「买入失败」列为一致性校验，"
        "正常应为 0（未成交样本在冻结阶段已剔除）。",
        "",
        f"## 组合总览 Top {TOP_SHOWN}"
        f"（按平均净收益率降序；全部 {len(results)} 个组合见 grid_results.csv）",
        "",
        md_table(
            overview_rows,
            ["组合", "族", "止盈分档", "T+1低开", "delta", "样本数", "净盈亏合计",
             "平均净收益率", "中位数", "胜率", "盈亏比", "收益/预算", "延期", "超期估值"],
        ),
        "",
        f"## 分年表现（Top {TOP_SHOWN}，平均净收益率，括号内为已卖出样本数）",
        "",
        md_table(matrix_rows, ["组合", *years, "正收益年数"]),
        "",
        "## 分档 × delta 矩阵（低开1，平均净收益率）",
        "",
        md_table(
            scheme_matrix_rows,
            ["分档", "族", *[dn for dn, _d in DELTA_GRID], "最优delta",
             f"{delta_last}-{delta_first}"],
        ),
        "",
        f"> 「最优delta」为该分档在低开1 下平均净收益率最高的 delta；"
        f"「{delta_last}-{delta_first}」为最深折价相对开盘买入的变化（敏感度）。"
        "低开2 的对应矩阵见 grid_results.csv；对角线（r=delta，卖出目标相对开盘价"
        "≈不变）见「关键单变量对比」。",
        "",
        "## delta 汇总（按 低开 × delta）",
        "",
        md_table(
            delta_rows,
            ["低开", "delta", "组合数", "全档平均", "固定阈值族平均", "全正阈值族平均",
             "含负阈值族平均", "主效应(对delta=0)", "最优组合"],
        ),
        "",
        "> 「主效应」= 该 delta 下全部分档相对 delta=0 的平均净收益率变化均值"
        "（同低开区间内逐分档配对），衡量折价本身的平均影响；「最优组合」跨两个低开区间。",
        "",
        "## 止盈档位分布（净收益率前 10 的组合，全部组合见 CSV）",
        "",
        md_table(
            tier_long_rows,
            ["组合", "止盈档位", "止盈阈值", "样本数", "已卖出",
             "平均净收益率", "胜率", "净盈亏合计"],
        ),
        "",
        "## 卖出方式分布（净收益率前 5 的组合，全部组合见 CSV）",
        "",
    ]
    for section in mode_sections:
        lines.extend(section)

    lines.extend(
        [
            "## 关键单变量对比",
            "",
            *comparison_lines,
            "",
            "> 第一组（分档/低开对比）固定在 delta=0，与历史轮次同口径，差异纯由离场"
            "规则或低开区间引起；对角线组保持卖出目标相对开盘价 ≈ 不变（r=delta），"
            "只加深买入折价，用于隔离「限价折价本身是否划算」；同分档加深组保持阈值"
            "相对买入成本不变。低开2 方向一致，明细见 CSV。",
            "",
            "## 结论",
            "",
            f"- 平均净收益率最高：**{best_avg['组合']}**"
            f"（{fmt_pct(best_avg['平均净收益率'])}，{best_avg['已卖出']} 笔已卖出，"
            f"胜率 {fmt_pct(best_avg['胜率'], signed=False)}）",
            f"- 净盈亏合计最高：**{best_total['组合']}**（{fmt_money(best_total['净盈亏合计'])} 元）",
            "- 各 delta 下最优：" + "；".join(best_per_delta),
            "- delta 主效应（低开1，跨全部分档平均）："
            + "；".join(
                f"{delta:g}% {fmt_pct(delta_effect.get((entry_first, delta_name)))}"
                for delta_name, delta in DELTA_GRID[1:]
                if delta_effect.get((entry_first, delta_name)) is not None
            )
            + "（正值代表折价平均改善收益）",
            f"- 对角线（卖出目标 ≈ 开盘价不变，只加深折价，固定 r=delta）：{diagonal_fixed}；"
            "若沿折价加深单调改善，说明折价收益超过成交率损失。",
            f"- 对角线（3 档阶梯，+0.5pp/+1pp 阈值配 0.5%/1% 折价）：{diagonal_ladder}"
            "（分档边界不随 delta 平移，样本归档有轻微漂移，参考意义弱于固定族对角线）。",
            f"- 分年最稳（{max_positive}/{len(years)} 年为正，并列 {len(most_stable)} 个）："
            + "、".join(
                f"{r['组合']}（{fmt_pct(r['平均净收益率'])}）" for r in most_stable[:5]
            )
            + ("……" if len(most_stable) > 5 else ""),
            f"- 动态分档最好 {best_dyn['组合']}（{fmt_pct(best_dyn['平均净收益率'])}）"
            f" vs 固定阈值最好 {best_fix['组合']}（{fmt_pct(best_fix['平均净收益率'])}）："
            f"动态{'高' if dyn_minus_fix >= 0 else '低'} {abs(dyn_minus_fix) * 100:.2f} 个百分点",
            f"- 低开1 × delta=0 下，含负阈值族平均 {fmt_pct(float(np.mean([r['平均净收益率'] or 0.0 for r in neg_on_e1])))}"
            f" vs 全正阈值族平均 {fmt_pct(float(np.mean([r['平均净收益率'] or 0.0 for r in pos_on_e1])))}"
            f"（差 {family_delta * 100:+.2f} 个百分点）",
            f"- 低开放宽到 [-11%, 0%] 平均为全部 {len(entry_deltas)} 个 (分档×delta) 拉低 "
            f"{fmt_pct(float(np.mean(entry_deltas)))} 的平均净收益率",
            "",
            "## 口径说明",
            "",
            "- 买入：delta=0 为 T+1 不复权开盘价；delta>0 为限价 "
            "(1+a-delta%)×close(T)（a=open(T+1)/close(T)-1，不复权），T+1 最低价触及"
            "基准价才成交（按基准价，未考虑排队与部分成交）。实际买入价 = 基准价×(1+滑点)。",
            "- 止盈分档涨幅 = close(T+1)/买入基准价 - 1（delta=0 时即原日内涨幅 "
            "close/open-1）；止盈目标价 = 买入基准价 × (1 + 该档止盈阈值)。目标价换回"
            "开盘价口径 ≈ 阈值 - delta，这是对角线对比（r=delta）的理论依据；a≠0 时存在二阶小偏差。",
            "- 卖出分支按不复权价格与分档参数逐样本重新判定：T+2 开盘高于目标价按开盘卖、"
            "盘中最高价触及目标价按目标价卖、否则收盘卖；收盘跌停顺延，超期按最后收盘估值"
            "（估值样本在总览中单列，不算已实现）。",
            "- 手数按预算 floor 取整；资金不足 1 手的样本买入数为 0 但仍计入样本数。",
            "- 同一交易日多个样本可能重叠，净盈亏合计不是组合净值，也不代表可同时投入的资金。",
            "- 手续费按成交金额计算，佣金有最低 5 元限制。",
            "- 样本冻结按 (低开 × delta) 进行：同组全部组合共用基准分档筛出的样本；"
            "若某分档的独立筛选会排除个别缺 T+2 最高价的样本，冻结口径下它们会以"
            "“未卖出”错误行出现并在总览可见。delta>0 的未成交样本在冻结阶段即被剔除，"
            "不进入该 delta 的任何组合。",
            "- all_trades.csv 默认只写 Top50 组合 ∪ 基准扫描组合"
            f"（{len(DELTA_ANCHOR_SCHEMES)} 个基准档 × 低开1 × 全部 delta），"
            "其余组合可用 limitup_shakeout_cash_returns.py 传对应参数复现（见下节）。",
            "- 逐笔分析列口径：买入日低开 = `open(T+1)/除权后昨收 - 1`（百分数，"
            "低开 3.3 个点记 -3.3，与低开筛选条件同口径）；行业取命中表快照；"
            "流通市值为 T+1 日 `daily_basic.circ_mv`（万元）；量能比 = T 日成交量 / "
            "T-1 日成交量（与策略“放量”条件同口径，成交量单位手）；涨停日 K 线实体 = "
            "`(T-1 收盘 - T-1 开盘) / T-1 开盘 × 100`（T-1 即涨停日，不复权价）。",
            "",
            "## 复现",
            "",
            "```powershell",
            "uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py",
            "",
            "# 单独复现某个组合（例：分档22 × 低开1 × delta 1.0）；各分档 JSON 见 run_meta.json",
            "uv run python tmp_scripts/limitup_shakeout_cash_returns.py "
            "--start 2018-11-01 --end latest \\",
            f"  --gap {DEFAULT_GAP[0]} {DEFAULT_GAP[1]} "
            f"--close-chg {DEFAULT_CLOSE_CHG[0]} {DEFAULT_CLOSE_CHG[1]} \\",
            f"  --rise {DEFAULT_RISE[0]} {DEFAULT_RISE[1]} "
            f"--rise-days {DEFAULT_RISE_DAYS} \\",
            f"  --entry-gap {ENTRY_GAP_GRID[0][1][0]} {ENTRY_GAP_GRID[0][1][1]} "
            "--require-low-support \\",
            f"  --exit-tiers \"{json.dumps(tiers_to_jsonable(next(t for n, t, f in EXIT_TIERS_GRID if n == '分档22')), ensure_ascii=False)}\" \\",
            "  --delta 1.0",
            "```",
            "",
            "## 输出文件",
            "",
            "- `report.md`：本报告",
            "- `grid_results.csv`：全部组合总览指标（含 delta 列与各分档 JSON）",
            "- `yearly_stats.csv`：各组合分年统计（长表，含 delta）",
            "- `tier_breakdown.csv`：各组合止盈档位分布（含 delta）",
            "- `sell_mode_breakdown.csv`：各组合卖出方式分布（含 delta）",
            "- `all_trades.csv`：Top50 ∪ 基准扫描组合的逐笔现金明细（含组合标签、delta，"
            "以及行业、买入日低开、买入日流通市值、量能比、涨停日 K 线实体等分析列）",
            "- `run_meta.json`：网格定义（含各分档 JSON、DELTA_GRID 与逐 (低开×delta) "
            "冻结统计）与运行参数",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args()
    if args.position <= 0:
        raise SystemExit("--position 必须 > 0")
    if args.max_delay_days < 0:
        raise SystemExit("--max-delay-days 必须 >= 0")
    if DEFAULT_TRADES_TOP_N < 0:
        raise SystemExit("DEFAULT_TRADES_TOP_N 必须 >= 0（0 = 全部组合写入明细）")
    scheme_tiers = {name: tiers for name, tiers, _family in EXIT_TIERS_GRID}
    scheme_family = {name: family for name, _tiers, family in EXIT_TIERS_GRID}
    for name, tiers in scheme_tiers.items():
        try:
            validate_exit_tiers(tiers)
        except ValueError as exc:
            raise SystemExit(f"网格中的止盈分档 {name} 无效：{exc}") from exc
    for name, rng in ENTRY_GAP_GRID:
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"网格中的低开区间 {name} 无效：{rng}")
    for delta_name, delta in DELTA_GRID:
        if delta < 0:
            raise SystemExit(f"网格中的限价深度 {delta_name} 无效：{delta}（必须 >= 0）")
    missing_anchors = [s for s in DELTA_ANCHOR_SCHEMES if s not in scheme_tiers]
    if missing_anchors:
        raise SystemExit(f"DELTA_ANCHOR_SCHEMES 引用了不存在的分档：{missing_anchors}")
    missing_verify = [s for s in VERIFY_SCHEMES if s not in scheme_tiers]
    if missing_verify:
        raise SystemExit(f"VERIFY_SCHEMES 引用了不存在的分档：{missing_verify}")

    start = filtered.compact_date(args.start)
    end = with_retry(lambda: cash.resolve_end_date(args.end), "解析结束日期")
    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")

    n_schemes = len(EXIT_TIERS_GRID)
    total_combos = n_schemes * len(ENTRY_GAP_GRID) * len(DELTA_GRID)
    print("=" * 90)
    print("limit_up_shakeout 现金收益网格搜索（止盈分档 × T+1 买入低开 × 限价深度）")
    print(f"  统计区间：{start} ~ {end}")
    print(f"  固定筛选：T日高开 {fmt_range(DEFAULT_GAP)}；T日收盘涨跌幅 {fmt_range(DEFAULT_CLOSE_CHG)}")
    print(f"  近{DEFAULT_RISE_DAYS}日涨幅 {fmt_range(DEFAULT_RISE)}；"
          f"信号日最低价>=前一日收盘：{'要求' if DEFAULT_REQUIRE_LOW_SUPPORT else '不要求'}")
    print(f"  网格：{n_schemes} 个止盈分档 × {len(ENTRY_GAP_GRID)} 个低开区间 × "
          f"{len(DELTA_GRID)} 个 delta = {total_combos} 个组合")
    for name, tiers, family in EXIT_TIERS_GRID:
        print(f"    {name}（{family}）：{format_tiers_summary(tiers)}")
    for name, rng in ENTRY_GAP_GRID:
        print(f"    {name}：{fmt_range(rng)}")
    for delta_name, delta in DELTA_GRID:
        meaning = "开盘价买入（原口径）" if delta == 0 else f"开盘下方 {delta:g}% 限价"
        print(f"    {delta_name}：{meaning}")
    print(f"  每笔预算：{args.position:,.0f} 元；手数模式：{args.lot_mode}；滑点：{args.slippage:.2%}")
    print(f"  样本冻结基准：{REFERENCE_SCHEME}（按 低开 × delta 分别冻结）；"
          f"校验轮廓：{'、'.join(VERIFY_SCHEMES)}")
    print("-" * 90)

    t0 = time.time()
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = with_retry(lambda: filtered.load_hits(start, end), "读取命中样本")
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条")

    calendar = with_retry(filtered.load_calendar, "读取交易日历")
    hits = filtered.attach_dates(hits, calendar, DEFAULT_RISE_DAYS)
    needed_dates = pd.concat(
        [
            hits["hit_date"],
            hits["prev_date"],
            hits["rise_ref_date"],
            hits["t1_date"],
            hits["t2_date"],
        ],
        ignore_index=True,
    ).dropna()
    if needed_dates.empty:
        raise SystemExit("无法定位任何交易日")
    min_date, max_date = str(needed_dates.min()), str(needed_dates.max())

    print(f"读取行情 {min_date} ~ {max_date} ...")
    prices = with_retry(
        lambda: filtered.load_prices(sorted(hits["ts_code"].unique()), min_date, max_date),
        "读取行情",
    )
    if prices.empty:
        raise SystemExit("没有读取到行情数据")
    print(f"行情行数：{len(prices):,}（耗时 {time.time() - t0:.1f}s）")
    print("-" * 90)

    def run_scheme_samples(
        entry_gap: tuple[float, float], delta: float, tiers: tuple
    ) -> pd.DataFrame:
        """跑一次完整筛选，返回全部命中样本框架（含 included 标记）。"""
        return filtered.build_samples(
            hits,
            prices,
            DEFAULT_GAP,
            DEFAULT_CLOSE_CHG,
            DEFAULT_RISE,
            DEFAULT_RISE_DAYS,
            tiers,
            entry_gap,
            delta_pct=delta,
            require_low_support=DEFAULT_REQUIRE_LOW_SUPPORT,
        )

    # 样本冻结：每个 (低开 × delta) 只筛一次，全部 33 个分档共用（保证纯离场规则
    # 对比）。delta>0 时样本集合依赖限价成交判定，必须逐 delta 冻结。
    frozen_input: dict[tuple[str, str], pd.DataFrame] = {}
    frozen_keys: dict[tuple[str, str], set] = {}
    frozen_stats: dict[tuple[str, str], dict] = {}
    base_samples: pd.DataFrame | None = None
    for entry_name, entry_gap in ENTRY_GAP_GRID:
        for delta_name, delta in DELTA_GRID:
            samples = run_scheme_samples(
                entry_gap, delta, scheme_tiers[REFERENCE_SCHEME]
            )
            included = samples[samples["included"]].copy()
            if included.empty:
                raise SystemExit(
                    f"低开区间 {entry_name} × delta {delta_name} "
                    "筛选后没有可计算收益的样本"
                )
            if base_samples is None:
                # 全量样本框架（含未纳入样本），供分析列构建使用；分析列只用
                # 与 delta 无关的列（entry_gap/prev_date/t1_date 等），取
                # (低开1, d0) 一次即可
                base_samples = samples
            cash_input = cash.build_cash_input(included).drop(
                columns=["收益率(后复权)"]
            )
            frozen_input[(entry_name, delta_name)] = cash_input
            frozen_keys[(entry_name, delta_name)] = set(
                zip(
                    cash_input["命中日期"].astype(str),
                    cash_input["股票代码"].astype(str),
                )
            )
            # 其余筛选全部满足、但限价未成交而被跳过的样本数（delta=0 时恒为 0）
            filters_mask = (
                samples["passed_gap"]
                & samples["passed_close"]
                & samples["passed_rise"]
                & samples["passed_entry_gap"]
            )
            if DEFAULT_REQUIRE_LOW_SUPPORT:
                filters_mask &= samples["passed_low_support"]
            unfilled_n = int((filters_mask & ~samples["buy_filled"]).sum())
            frozen_stats[(entry_name, delta_name)] = {
                "samples": len(cash_input),
                "unfilled": unfilled_n,
            }
            print(
                f"低开 {entry_name} × delta {delta_name}（{delta:g}%）："
                f"冻结样本 {len(cash_input):,} 条"
                f"（其余筛选满足但限价未成交 {unfilled_n:,} 条；基准 {REFERENCE_SCHEME}）"
            )

    # 校验：极端阈值轮廓独立重筛，对比各冻结样本集合差异（应为 0）
    verify_diffs: dict[str, int] = {}
    for entry_name, entry_gap in ENTRY_GAP_GRID:
        for delta_name, delta in DELTA_GRID:
            for vname in VERIFY_SCHEMES:
                vsamples = run_scheme_samples(
                    entry_gap, delta, scheme_tiers[vname]
                )
                vincluded = vsamples[vsamples["included"]].copy()
                vkeys = set(
                    zip(
                        vincluded["hit_date"].astype(str),
                        vincluded["ts_code"].astype(str),
                    )
                )
                diff = len(vkeys ^ frozen_keys[(entry_name, delta_name)])
                verify_diffs[f"{vname}×{entry_name}×{delta_name}"] = diff
                if diff:
                    print(
                        f"  注意：{vname}×{entry_name}×{delta_name} "
                        f"独立重筛与冻结集合差异 {diff} 条"
                    )
    if all(v == 0 for v in verify_diffs.values()):
        print(f"冻结样本校验通过：{len(verify_diffs)} 个极端轮廓重筛差异均为 0 条")

    # 逐笔分析列：行业 / 买入日低开 / 流通市值 / 量能比 / 涨停日 K 线实体
    enrich = build_enrichment(base_samples, prices, start, end)
    print(
        f"分析列构建完成：{len(enrich):,} 行"
        f"（低开/行业/流通市值/量能比/涨停日实体）"
    )

    # 预取顺延行情并注入：组合循环阶段零数据库访问，不受网络抖动影响
    delay_cache = preload_delay_cache(frozen_input, calendar, args.max_delay_days)
    print("-" * 90)

    cash_args_base = dict(
        position=args.position,
        lot_mode=args.lot_mode,
        max_delay_days=args.max_delay_days,
        commission=args.commission,
        min_commission=args.min_commission,
        stamp=args.stamp,
        transfer=args.transfer,
        slippage=args.slippage,
    )
    entry_gap_map = {name: rng for name, rng in ENTRY_GAP_GRID}

    results: list[dict] = []
    yearly: dict[str, list[dict]] = {}
    tier_breakdown: dict[str, list[dict]] = {}
    sell_mode: dict[str, list[dict]] = {}

    # 第一遍：只算各组合指标，不驻留逐笔明细（330 个组合全量驻留会占用过多内存；
    # all_trades.csv 的入选组合等指标齐全后第二遍补算并增量写盘）
    t_combo = time.time()
    done = 0
    for tier_name, tiers, family in EXIT_TIERS_GRID:
        for entry_name in entry_gap_map:
            for delta_name, delta in DELTA_GRID:
                combo_id = f"{tier_name}×{entry_name}×{delta_name}"
                cash_args = argparse.Namespace(**cash_args_base, delta=delta)
                trades = cash.calc_trades(
                    frozen_input[(entry_name, delta_name)], cash_args, tiers,
                    calendar=calendar, price_cache=delay_cache,
                )
                metrics = combo_metrics(
                    combo_id, tier_name, family, tiers, entry_name,
                    entry_gap_map[entry_name], delta, delta_name, trades, args,
                )
                results.append(metrics)
                yearly[combo_id] = yearly_rows(trades)
                tier_breakdown[combo_id] = tier_breakdown_rows(trades, tiers)
                sell_mode[combo_id] = sell_mode_rows(trades)
                done += 1
                print(
                    f"{combo_id}：样本 {metrics['样本数']:,}，"
                    f"净盈亏合计 {fmt_money(metrics['净盈亏合计'])} 元，"
                    f"平均净收益率 {fmt_pct(metrics['平均净收益率'])}，"
                    f"胜率 {fmt_pct(metrics['胜率'], signed=False)}"
                    f"（{done}/{total_combos}，{time.time() - t_combo:.0f}s）"
                )

    print("-" * 90)
    ranked = sorted(
        results,
        key=lambda r: (r["平均净收益率"] is None, r["平均净收益率"] or 0.0),
        reverse=True,
    )
    metrics_index = {r["组合"]: r for r in results}
    print(f"按平均净收益率排名（前 10 / 共 {len(ranked)}）：")
    for i, r in enumerate(ranked[:10], start=1):
        print(
            f"  {i}. {r['组合']}：{fmt_pct(r['平均净收益率'])}，"
            f"净盈亏合计 {fmt_money(r['净盈亏合计'])} 元，胜率 {fmt_pct(r['胜率'], signed=False)}"
        )

    # delta 主效应与成交留存（低开1，跨全部分档平均）
    entry_first_name = ENTRY_GAP_GRID[0][0]
    delta_first_name = DELTA_GRID[0][0]
    base_avg = {
        r["分档"]: r["平均净收益率"]
        for r in results
        if r["低开"] == entry_first_name and r["delta名"] == delta_first_name
    }
    base_st = frozen_stats.get((entry_first_name, delta_first_name)) or {}
    effect_parts = []
    for delta_name, delta in DELTA_GRID[1:]:
        subs = [
            r for r in results
            if r["低开"] == entry_first_name and r["delta名"] == delta_name
        ]
        diffs = [
            r["平均净收益率"] - base_avg[r["分档"]]
            for r in subs
            if r["分档"] in base_avg
            and r["平均净收益率"] is not None
            and base_avg[r["分档"]] is not None
        ]
        st = frozen_stats.get((entry_first_name, delta_name)) or {}
        retention = (
            f"，留存率 {st['samples'] / base_st['samples']:.1%}"
            if st.get("samples") and base_st.get("samples")
            else ""
        )
        if diffs:
            effect_parts.append(
                f"{delta:g}%：{float(np.mean(diffs)) * 100:+.2f} pp{retention}"
            )
    if effect_parts:
        print("delta 主效应（低开1，跨全部分档相对开盘买入的平均变化）：" + "；".join(effect_parts))

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_cash_grid_search_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        grid_csv_rows = []
        for r in ranked:
            grid_csv_rows.append(
                {
                    "组合": r["组合"],
                    "delta": r["delta"],
                    "族": r["族"],
                    "止盈分档": format_tiers_summary(r["tiers"]),
                    "止盈分档JSON": json.dumps(
                        tiers_to_jsonable(r["tiers"]), ensure_ascii=False
                    ),
                    "T+1低开区间": fmt_range(r["entry_gap"]),
                    "样本数": r["样本数"],
                    "实际买入": r["实际买入"],
                    "买入失败": r["买入失败"],
                    "已卖出": r["已卖出"],
                    "延期卖出": r["延期卖出"],
                    "超期估值": r["超期估值"],
                    "未卖出": r["未卖出"],
                    "净盈亏合计": r["净盈亏合计"],
                    "平均净盈亏": r["平均净盈亏"],
                    "平均净收益率": r["平均净收益率"],
                    "中位数": r["中位数"],
                    "胜率": r["胜率"],
                    "盈亏比": r["盈亏比"],
                    "收益/预算": r["收益/预算"],
                }
            )
        pd.DataFrame(grid_csv_rows).to_csv(
            run_dir / "grid_results.csv", index=False, encoding="utf-8-sig"
        )

        yearly_csv_rows = [
            {"组合": r["组合"], "delta": r["delta"], **row}
            for r in ranked
            for row in yearly[r["组合"]]
        ]
        pd.DataFrame(yearly_csv_rows).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )

        tier_csv_rows = [
            {"组合": r["组合"], "delta": r["delta"], **row}
            for r in ranked
            for row in tier_breakdown[r["组合"]]
        ]
        pd.DataFrame(tier_csv_rows).to_csv(
            run_dir / "tier_breakdown.csv", index=False, encoding="utf-8-sig"
        )

        mode_csv_rows = [
            {"组合": r["组合"], "delta": r["delta"], **row}
            for r in ranked
            for row in sell_mode[r["组合"]]
        ]
        pd.DataFrame(mode_csv_rows).to_csv(
            run_dir / "sell_mode_breakdown.csv", index=False, encoding="utf-8-sig"
        )

        # ---- all_trades.csv：Top-N ∪ 基准扫描组合，第二遍补算并增量写盘 ----
        top_n = DEFAULT_TRADES_TOP_N if DEFAULT_TRADES_TOP_N > 0 else len(ranked)
        selected_ids = [r["组合"] for r in ranked[:top_n]]
        selected_set = set(selected_ids)
        for anchor in DELTA_ANCHOR_SCHEMES:
            for delta_name, _delta in DELTA_GRID:
                cid = f"{anchor}×{ENTRY_GAP_GRID[0][0]}×{delta_name}"
                if cid in metrics_index and cid not in selected_set:
                    selected_ids.append(cid)
                    selected_set.add(cid)
        print(
            f"写 all_trades.csv：{len(selected_ids)} / {len(results)} 个组合"
            f"（Top{top_n} ∪ 基准扫描组合）..."
        )
        # 分析列排在前部，便于后续透视；其余列保持原顺序
        preferred = [
            "组合", "delta", "命中日期", "股票代码", "股票名称", "行业",
            "买入日低开(%)", "买入日流通市值(万元)", "量能比(T/T-1)",
            "涨停日K线实体(%)", "买入日", "计划卖出日", "实际卖出日",
            "T+1收盘对买入价涨幅", "动态止盈阈值", "止盈区间", "卖出方式",
        ]
        with (run_dir / "all_trades.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as fout:
            wrote_header = False
            for i, cid in enumerate(selected_ids, start=1):
                m = metrics_index[cid]
                cash_args = argparse.Namespace(**cash_args_base, delta=m["delta"])
                trades = cash.calc_trades(
                    frozen_input[(m["低开"], m["delta名"])], cash_args, m["tiers"],
                    calendar=calendar, price_cache=delay_cache,
                )
                trades = trades.merge(enrich, on=["hit_date", "ts_code"], how="left")
                trades.insert(0, "combo", cid)
                trades.insert(1, "delta", m["delta"])
                out = trades.rename(columns={"combo": "组合", **cash.TRADE_CN})
                order = [c for c in preferred if c in out.columns] + [
                    c for c in out.columns if c not in preferred
                ]
                out[order].to_csv(fout, header=not wrote_header, index=False)
                wrote_header = True
                print(f"  [明细 {i}/{len(selected_ids)}] {cid}")

        meta = {
            "generated_at": generated_at,
            "start": start,
            "end": end,
            "fixed": {
                "gap_range": list(DEFAULT_GAP),
                "close_chg_range": list(DEFAULT_CLOSE_CHG),
                "rise_range": list(DEFAULT_RISE),
                "rise_days": DEFAULT_RISE_DAYS,
                "require_low_support": DEFAULT_REQUIRE_LOW_SUPPORT,
            },
            "exit_tiers_grid": [
                {
                    "name": name,
                    "family": family,
                    "tiers": tiers_to_jsonable(tiers),
                }
                for name, tiers, family in EXIT_TIERS_GRID
            ],
            "entry_gap_grid": [
                {"name": name, "range": list(rng)} for name, rng in ENTRY_GAP_GRID
            ],
            "delta_grid": [
                {"name": name, "pct": pct} for name, pct in DELTA_GRID
            ],
            "delta_anchor_schemes": list(DELTA_ANCHOR_SCHEMES),
            "frozen_samples": {
                "reference_scheme": REFERENCE_SCHEME,
                "verify_schemes": list(VERIFY_SCHEMES),
                "verify_diffs": verify_diffs,
                "per_entry_delta": {
                    f"{entry_name}×{delta_name}": {
                        "delta": delta,
                        "samples": st["samples"],
                        "unfilled": st["unfilled"],
                    }
                    for (entry_name, delta_name), st in frozen_stats.items()
                },
            },
            "trades_top_n": DEFAULT_TRADES_TOP_N,
            "trades_combos": selected_ids,
            "analysis_columns": [
                "行业", "买入日低开(%)", "买入日流通市值(万元)",
                "量能比(T/T-1)", "涨停日K线实体(%)",
            ],
            "cash": {
                "position": args.position,
                "lot_mode": args.lot_mode,
                "max_delay_days": args.max_delay_days,
                "commission": args.commission,
                "min_commission": args.min_commission,
                "stamp": args.stamp,
                "transfer": args.transfer,
                "slippage": args.slippage,
            },
            "hit_samples": len(hits),
            "price_rows": len(prices),
            "results": [
                {
                    "组合": r["组合"],
                    "族": r["族"],
                    "delta": r["delta"],
                    "样本数": r["样本数"],
                    "净盈亏合计": r["净盈亏合计"],
                    "平均净收益率": r["平均净收益率"],
                    "胜率": r["胜率"],
                }
                for r in ranked
            ],
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        report = build_report(
            args, start, end, results, yearly, tier_breakdown, sell_mode,
            frozen_stats, verify_diffs, generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（--no-report）")
    print(f"总耗时：{time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
