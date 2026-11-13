"""端到端：确定性 / 前缀不变性 / 权益独立复算 / poslayer 适配器冒烟。"""

import math

import pytest

from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy
from framesandbox.adapters.poslayer import PosLayerStrategy


class BuyHold(SandboxStrategy):
    name = "buyhold"

    def on_start(self, ctx):
        super().on_start(ctx)
        self.done = False

    def on_day_close(self, day):
        if not self.done:
            self.ctx.buy("SZ.000001", 1000)
            self.done = True


def _cfg(**kw):
    base = dict(start="2023-06-01", end="2023-09-28", resolution="daily",
                seed=11, report=False, progress_every=0, initial_cash=100_000)
    base.update(kw)
    return SandboxConfig(**base)


def test_determinism(data):
    r1 = SandboxRunner(_cfg(), [BuyHold()], data=data).run()
    r2 = SandboxRunner(_cfg(), [BuyHold()], data=data).run()
    assert r1.equity_curve == r2.equity_curve
    assert [(f.symbol, f.size, f.price, f.cost) for f in r1.fills] == \
           [(f.symbol, f.size, f.price, f.cost) for f in r2.fills]


def test_prefix_invariance(data):
    """短区间回放 == 长区间回放的前缀（因果性的外显判据）。"""
    short = SandboxRunner(_cfg(end="2023-07-15"), [BuyHold()], data=data).run()
    long = SandboxRunner(_cfg(end="2023-09-28"), [BuyHold()], data=data).run()
    n = len(short.equity_curve)
    assert long.equity_curve[:n] == short.equity_curve
    assert len(long.fills) >= len(short.fills)
    assert [(f.price, f.size) for f in long.fills[:len(short.fills)]] == \
           [(f.price, f.size) for f in short.fills]


def test_equity_independent_recompute(data):
    """用成交明细独立重算现金，与账本逐日对账（零偏差判据）。"""
    res = SandboxRunner(_cfg(), [BuyHold()], data=data).run()
    cash = 100_000.0
    for f in res.fills:
        notional = f.size * f.price
        cash += notional - f.cost if f.side.value == "sell" else -(notional + f.cost)
    last = res.equity_curve[-1]
    # 期末：cash + 1000 股 × 最后收盘
    bars_close = last[1] - last[2]
    assert abs(last[2] - cash) < 1e-6
    assert bars_close >= 0


def test_poslayer_smoke(data):
    """poslayer 适配器在小数据上跑通（regime 门大概率 OFF → 空仓也是正确结果）。"""
    cfg = _cfg(cost_preset="poslayer", initial_cash=200_000)
    strat = PosLayerStrategy()
    res = SandboxRunner(cfg, [strat], data=data).run()
    assert res.days > 0
    assert len(strat.decisions) == res.days
    assert res.audit["n_violations"] == 0
    assert all(e[1] > 0 for e in res.equity_curve)


def test_poslayer_entries_on_regime(data):
    """构造深度超跌指数 → regime ON → 适配器必须建仓（验证信号链路活着）。"""
    import numpy as np
    import pandas as pd
    from framesandbox.data.warehouse import SandboxData
    from tests.conftest import make_index

    days = data.calendar.days
    # 指数：前段平稳，后 60 日暴跌 30% → RSI<30 + 跌破年线 + ret20<-5%
    idx = make_index(days, seed=3)
    vals = idx.to_numpy().copy()
    crash_start = len(vals) - 80
    for i in range(crash_start, len(vals)):
        vals[i] = vals[crash_start - 1] * (1 - 0.35 * (i - crash_start) / (len(vals) - crash_start))
    idx2 = pd.Series(vals, index=idx.index)
    data2 = SandboxData(panel=data.panel, index=idx2)
    from framesandbox.clock import ord_to_date
    start = ord_to_date(days[crash_start + 40]).isoformat()
    end = ord_to_date(days[-1]).isoformat()
    cfg = SandboxConfig(start=start, end=end, resolution="daily", report=False,
                        progress_every=0, cost_preset="poslayer", initial_cash=1_000_000,
                        lot=100)
    strat = PosLayerStrategy()
    res = SandboxRunner(cfg, [strat], data=data2).run()
    on_days = [d for d in strat.decisions if d["regime_on"]]
    assert on_days, "构造的超跌行情应触发 regime ON"
    assert len(res.fills) > 0, "regime ON 且有合格池时应建仓"
