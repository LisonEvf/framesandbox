"""适配器口径对账 + 盘中订阅同步。

test_poslayer_features_match_live_baseline 需要真实面板（缺失自动跳过）：
验证沙盒适配器的滚动特征与 live/strategy.precompute 在同一天逐标的相等 ——
「同源同口径」不是自述，是对账出来的。
"""

from pathlib import Path

import numpy as np
import pytest

from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy
from framesandbox.adapters.poslayer import PosLayerStrategy
from framesandbox.clock import minute_ts

_REAL_PANEL = Path(__file__).resolve().parents[2] / "framebt" / "tools" / "cache" / "daily_panel_v2.parquet"


def test_midday_subscribe_sync(data):
    """盘中订阅：立即同步到当前分钟，历史不泄露未来，后续分钟事件正常到达。"""

    class MidSub(SandboxStrategy):
        name = "midsub"

        def on_start(self, ctx):
            super().on_start(ctx)
            self.done = False
            self.late_quotes = 0
            self.hist_ok = False

        def on_quote(self, q):
            if not self.done and q.minute_of_day >= 600:   # 10:00
                self.done = True
                self.ctx.subscribe("SH.600004")
                bars = self.ctx.data.history("SH.600004", "1m", 500)
                assert bars, "订阅后应能取到今日已完成分钟"
                assert all(b.timestamp <= minute_ts(q.day_ord, q.minute_of_day) for b in bars)
                self.hist_ok = True
            if q.symbol == "SH.600004" and q.minute_of_day > 600:
                self.late_quotes += 1

    cfg = SandboxConfig(start="2023-06-01", end="2023-06-02", resolution="1m",
                        focus=["SZ.000001"], report=False, progress_every=0)
    strat = MidSub()
    SandboxRunner(cfg, [strat], data=data).run()
    assert strat.hist_ok
    assert strat.late_quotes > 100      # 订阅后剩余分钟全部到达


@pytest.mark.skipif(not _REAL_PANEL.exists(), reason="需要真实面板 daily_panel_v2.parquet")
def test_poslayer_features_match_live_baseline():
    """沙盒适配器特征 == live/strategy.precompute 同日特征（逐标的对账）。"""
    import sys
    fb = Path(__file__).resolve().parents[2] / "framebt"
    sys.path.insert(0, str(fb / "live"))
    sys.path.insert(0, str(fb / "tools"))
    import strategy as live_st

    from framesandbox.data.warehouse import SandboxData
    data = SandboxData()
    cutoff = "2024-06-28"

    class Capture(PosLayerStrategy):
        def on_day_close(self, day):
            super().on_day_close(day)
            self.captured = dict(self._features())
            self.cols = list(self._cols)

    strat = Capture()
    cfg = SandboxConfig(start=cutoff, end=cutoff, resolution="daily", report=False,
                        progress_every=0, cost_preset="poslayer")
    SandboxRunner(cfg, [strat], data=data).run()

    panel = live_st.lab.load_panel()
    P = live_st.precompute(panel)
    d0 = live_st.lab.ord_day(cutoff)
    i = int(np.searchsorted(P["dates"], d0))
    assert P["dates"][i] == d0

    cap = strat.captured
    base_cols = P["cols"]
    for name in ("rev5", "amt20", "vratio", "hist", "sane"):
        a = np.array([cap[name][strat._col_ix[s]] if s in strat._col_ix else np.nan
                      for s in base_cols], dtype=float)
        b = P[name][i].astype(float)
        both = np.isfinite(a) & np.isfinite(b)
        assert both.sum() > 4000, f"{name}: 有效样本过少"
        if name == "sane":
            assert np.array_equal(a[both].astype(bool), b[both].astype(bool)), name
        else:
            rel = np.abs(a[both] - b[both]) / np.maximum(1e-9, np.abs(b[both]))
            assert np.max(rel) < 1e-9, f"{name}: 最大相对偏差 {np.max(rel)}"
