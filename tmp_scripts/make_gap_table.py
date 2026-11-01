from __future__ import annotations

import pandas as pd

d = pd.read_csv(
    "tmp_scripts/uptrend_gap_strict.csv",
    dtype={"命中日期": str, "前一日": str, "代码": str},
)
d = d.sort_values(["命中日期", "代码"])

lines = [
    "| 日期 | 代码 | 名称 | 前日 | 前日跳空 | 前日涨跌 | 当日跳空 | 当日涨跌 | 当日开/低/收 | 前日收绿 | 当日阴线 | 成交额(亿) |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|",
]
for _, r in d.iterrows():
    lines.append(
        f"| {r['命中日期']} | {r['代码']} | {r['名称']} | {r['前一日']} | "
        f"{r['前日跳空幅度']*100:.2f}% | {r['前日涨跌幅']*100:.2f}% | "
        f"{r['当日跳空幅度']*100:.2f}% | {r['当日涨跌幅']*100:.2f}% | "
        f"{r['当日开盘']:.2f}/{r['当日最低']:.2f}/{r['当日收盘']:.2f} | "
        f"{'✓' if r['前日收绿'] else '✗'} | {'✓' if r['当日阴线'] else '✗'} | "
        f"{r['成交额(千元)']/1e5:.2f} |"
    )

text = "\n".join(lines)
with open("tmp_scripts/uptrend_gap_table.md", "w", encoding="utf-8") as f:
    f.write(text)
print(len(d))
