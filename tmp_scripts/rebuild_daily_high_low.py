"""用 Tushare 分钟行情重算 daily.high / daily.low 的薄入口。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from quant.data.daily_high_low import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
