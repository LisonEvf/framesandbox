"""pytest 共享夹具：内存合成数据（不依赖 350MB 真实面板）。"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def trading_days(start: str, n: int) -> list[int]:
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d - date(1970, 1, 1)).days)   # UNIX 天数（面板约定）
        d += timedelta(days=1)
    return out


def make_panel(symbols=("SZ.000001", "SZ.000002", "SH.600000", "SH.600004",
                        "SZ.300001", "SH.688001"),
               start="2022-01-03", n_days=520, seed=7) -> pd.DataFrame:
    """确定性随机游走日线面板（含趋势/波动差异，供假突破自然发生）。"""
    rng = np.random.default_rng(seed)
    days = trading_days(start, n_days)
    rows = []
    for si, sym in enumerate(symbols):
        px = 10.0 + si * 3
        drift = 0.0004 * (1 if si % 2 == 0 else -1)
        for d in days:
            ret = rng.normal(drift, 0.018 + 0.004 * (si % 3))
            o = px
            c = max(1.0, px * (1 + ret))
            h = max(o, c) * (1 + abs(rng.normal(0, 0.008)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.008)))
            vol = float(rng.integers(2_000_000, 20_000_000))
            amt = vol * (o + c) / 2
            rows.append((d, sym, round(o, 4), round(h, 4), round(l, 4), round(c, 4),
                         vol, round(amt, 2), 1e8, 0.01))
            px = c
    return pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close",
                                       "vol", "amount", "float_shares", "turnover"])


def make_index(days: list[int], seed=3) -> pd.Series:
    rng = np.random.default_rng(seed)
    vals, px = [], 3000.0
    for _ in days:
        px *= 1 + rng.normal(0.0001, 0.012)
        vals.append(px)
    idx = pd.DatetimeIndex([date(1970, 1, 1) + timedelta(days=d) for d in days])
    return pd.Series(vals, index=idx)


@pytest.fixture
def panel():
    return make_panel()


@pytest.fixture
def data(panel):
    from framesandbox.data.warehouse import SandboxData
    days = sorted(panel["date"].unique().tolist())
    return SandboxData(panel=panel, index=make_index(days))
