"""假信号检测与对抗注入。"""

import pytest

from framesandbox import (AdversarialInjector, SandboxConfig, SandboxRunner,
                          SandboxStrategy, SignalSpec)
from framesandbox.data.replay import ReplayTimeline
from framesandbox.data.synth import synth_minute_path


class BreakoutChaser(SandboxStrategy):
    """演示策略：盘中突破 20 日高就追买，收盘前无条件平仓 —— 天然会被假突破骗。"""

    name = "chaser"

    def on_start(self, ctx):
        super().on_start(ctx)
        self.bought_today = set()
        for s in ("SZ.000001", "SH.600000"):
            ctx.subscribe(s)

    def on_day_open(self, day):
        self.bought_today = set()
        self.level = {}
        for s in ("SZ.000001", "SH.600000"):
            bars = self.ctx.data.history(s, "1d", 20)
            if bars:
                self.level[s] = max(b.high for b in bars)

    def on_quote(self, q):
        lv = self.level.get(q.symbol)
        if lv and q.last > lv and q.symbol not in self.bought_today:
            self.bought_today.add(q.symbol)
            self.ctx.buy(q.symbol, 1000)

    def on_day_close(self, day):
        for s in list(self.bought_today):
            avail = self.ctx.broker.available_to_sell(s, day["ord"])
            # T+1：当日买入不可卖 → 持有到次日（更真实的「被套」体验）


def test_detector_finds_fakes(data):
    cfg = SandboxConfig(start="2023-06-01", end="2023-09-28", resolution="1m",
                        focus=["SZ.000001", "SH.600000"], seed=11, report=False,
                        progress_every=0, specs=(SignalSpec("breakout", n=20),))
    res = SandboxRunner(cfg, [BreakoutChaser()], data=data).run()
    dec = res.deception
    assert dec["episodes"] > 0
    assert dec["fake"] + dec["held"] == dec["episodes"]
    assert 0 <= dec["fake_rate"] <= 1
    # 追单关联：chaser 的买单应被标记为 chased
    assert dec["chased"] > 0
    assert any(e["chased"] for e in res.episodes)


def test_injector_only_alters_synthetic(data):
    inj = AdversarialInjector(prob=1.0, n=20, seed=5)
    days = data.calendar.days[100:160]
    tl = ReplayTimeline(data, start_ord=days[0], end_ord=days[-1], seed=11, injector=inj)
    altered = 0
    for day in tl.days[:20]:
        for sym in ("SZ.000001", "SH.600000"):
            path = day.path(sym)
            assert path is not None
            if path.source == "altered":
                altered += 1
                # 一致性：日线 high == 路径最高价
                assert day.bar(sym).high == max(path.prices)
                assert day.altered[sym]
            else:
                assert path.source == "synthetic"   # 小数据无真实分钟
    assert altered > 0
    assert len(inj.injections) == altered


def test_injector_deterministic(data):
    days = data.calendar.days[100:160]
    def run():
        inj = AdversarialInjector(prob=1.0, seed=5)
        tl = ReplayTimeline(data, start_ord=days[0], end_ord=days[-1], seed=11, injector=inj)
        return [(i["day"], i["symbol"], i["peak"]) for i in inj.injections]
    assert run() == run()


def test_stress_run_report(data):
    cfg = SandboxConfig(start="2023-06-01", end="2023-07-31", resolution="1m",
                        focus=["SZ.000001"], seed=3, report=False, progress_every=0,
                        stress={"prob": 0.5, "seed": 9})
    res = SandboxRunner(cfg, [BreakoutChaser()], data=data).run()
    assert len(res.injections) > 0
    assert all("stress" in i["note"] for i in res.injections)
