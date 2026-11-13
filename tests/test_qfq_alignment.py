"""真实分钟路径的 QFQ 口径对齐（开盘锚点缩放）。

数仓分钟价 = 不复权原始价；日线 = QFQ。除权除息日两者错位会制造虚假信号。
回放必须把分钟路径缩放到日线口径（factor = daily_open / raw_first_minute）。
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from framesandbox.calendar import SESSION_MINUTES
from framesandbox.data.replay import ReplayTimeline
from framesandbox.data.warehouse import SandboxData
from tests.conftest import make_index


def test_real_path_scaled_to_qfq(panel):
    days = sorted(panel["date"].unique().tolist())
    d = days[100]
    sym = "SZ.000001"
    bar = panel[(panel["date"] == d) & (panel["symbol"] == sym)].iloc[0]
    # 注入「原始价」分钟数据：价格 = 日线开盘 × 1.4（模拟除权错位），盘中 +2%
    rows = []
    for i, mi in enumerate(SESSION_MINUTES):
        p = bar.open * 1.4 * (1 + 0.02 * (i / 239))
        rows.append({"symbol_key": sym, "minute": mi, "price": p, "avg": p, "vol": 1000.0})
    ds = (date(1970, 1, 1) + timedelta(days=int(d))).isoformat()
    data = SandboxData(panel=panel, index=make_index(days),
                       intraday_override={ds: pd.DataFrame(rows)})
    tl = ReplayTimeline(data, start_ord=d, end_ord=d, seed=0)
    path = tl.day(d).path(sym)
    assert path.source == "real"
    # 开盘锚点精确对齐 QFQ 日线 open
    assert path.prices[0] == pytest.approx(bar.open, abs=1e-3)
    # 全路径按同一因子缩放（原始 +2% 形态保持）
    assert path.prices[-1] == pytest.approx(bar.open * 1.02, rel=1e-3)
    # 量不缩放（未复权原始量与 QFQ 日线 vol 同口径）
    assert path.vols[0] == 1000.0


def test_no_corporate_action_factor_one(panel):
    """无除权错位时（原始价=日线价），缩放因子=1，路径不变。"""
    days = sorted(panel["date"].unique().tolist())
    d = days[100]
    sym = "SZ.000001"
    bar = panel[(panel["date"] == d) & (panel["symbol"] == sym)].iloc[0]
    rows = [{"symbol_key": sym, "minute": mi, "price": bar.open, "avg": bar.open,
             "vol": 500.0} for mi in SESSION_MINUTES]
    ds = (date(1970, 1, 1) + timedelta(days=int(d))).isoformat()
    data = SandboxData(panel=panel, index=make_index(days),
                       intraday_override={ds: pd.DataFrame(rows)})
    tl = ReplayTimeline(data, start_ord=d, end_ord=d, seed=0)
    path = tl.day(d).path(sym)
    assert all(p == pytest.approx(bar.open, abs=1e-3) for p in path.prices)
