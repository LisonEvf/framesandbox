"""前视攻击测试：网关 G1–G6 不变式（沙盒的核心承诺）。"""

import pytest

from framesandbox import (LookaheadViolation, SandboxConfig, SandboxRunner,
                          SandboxStrategy)
from framesandbox.clock import minute_ts


class Probe(SandboxStrategy):
    """在回放中持续攻击网关，记录一切异常与供出的最大时间戳。"""

    name = "probe"

    def on_start(self, ctx):
        super().on_start(ctx)
        self.max_daily_ts = 0
        self.max_minute_ts = 0
        self.violations_caught = 0
        self.clock_violations = 0
        self.panel_future_rows = 0
        self.copy_mutations_ok = 0
        self.attack_log = []
        # 盘前（09:15）forming 应为 None
        self.forming_before_open = ctx.data.forming("SZ.000001")

    def on_day_open(self, day):
        d = self.ctx.data
        # 攻击 1：显式请求未来日线
        try:
            d.history("SZ.000001", "1d", 10, end=day["ord"] + 30)
        except LookaheadViolation:
            self.violations_caught += 1
        # 攻击 2：panel as_of 未来
        try:
            d.panel(as_of=day["ord"] + 10)
        except LookaheadViolation:
            self.violations_caught += 1
        # 攻击 3：负窗口
        try:
            d.history("SZ.000001", "1d", -5)
        except LookaheadViolation:
            self.violations_caught += 1
        # 攻击 4：时钟篡改（ClockWriteViolation，不计入网关数据违规）
        from framesandbox.errors import ClockWriteViolation
        try:
            self.ctx.clock.set_now(0)
        except ClockWriteViolation:
            self.clock_violations += 1

    def on_quote(self, q):
        d = self.ctx.data
        now = d.now()
        now_m = now.hour * 60 + now.minute
        assert q.ts_us <= d.now_us()
        assert q.minute_of_day <= now_m or q.minute_of_day == -1
        self.max_minute_ts = max(self.max_minute_ts, minute_ts(q.day_ord, max(q.minute_of_day, 0)))
        bars = d.history(q.symbol, "1m", 30)
        for b in bars:
            assert b.timestamp <= minute_ts(q.day_ord, now_m)
            self.max_minute_ts = max(self.max_minute_ts, b.timestamp)

    def on_day_close(self, day):
        d = self.ctx.data
        # G1：日线历史不含今日之前未完成的任何未来
        for sym in ("SZ.000001", "SH.600000"):
            bars = d.history(sym, "1d", 400)
            assert bars, sym
            assert bars[-1].timestamp <= day["ord"]
            self.max_daily_ts = max(self.max_daily_ts, bars[-1].timestamp)
            # 升序
            ts = [b.timestamp for b in bars]
            assert ts == sorted(ts)
        # 面板截断：最大日期 <= 今日
        p = d.panel(n_days=50)
        assert int(p["date"].max()) <= day["ord"]
        if int(p["date"].max()) > day["ord"]:
            self.panel_future_rows += 1
        # G3：副本隔离 —— 篡改返回值不影响引擎
        p2 = d.panel(n_days=5)
        before = int(p2["date"].max())
        p2.loc[0, "close"] = -999.0
        p3 = d.panel(n_days=5)
        if float(p3.loc[0, "close"]) != -999.0:
            self.copy_mutations_ok += 1
        # 指数截断
        s = d.index_series()
        from datetime import date as _d
        assert (s.index[-1].date() - _d(1970, 1, 1)).days <= day["ord"]


def _cfg(**kw):
    base = dict(start="2023-06-01", end="2023-09-28", resolution="daily",
                seed=11, report=False, initial_cash=1e6, progress_every=0)
    base.update(kw)
    return SandboxConfig(**base)


def test_pit_daily(data):
    probe = Probe()
    res = SandboxRunner(_cfg(), [probe], data=data).run()
    assert probe.violations_caught >= 3 * 10          # 每交易日至少 3 次数据攻击被拦
    assert probe.clock_violations >= 10
    assert probe.panel_future_rows == 0
    assert probe.copy_mutations_ok > 0
    assert probe.forming_before_open is None
    assert res.audit["n_violations"] == probe.violations_caught
    assert res.summary()["lookahead_violations"] > 0  # 攻击被记录在案


def test_pit_intraday(data):
    probe = Probe()
    res = SandboxRunner(_cfg(resolution="1m", focus=["SZ.000001", "SH.600000"],
                             start="2023-06-01", end="2023-06-30"),
                        [probe], data=data).run()
    assert probe.violations_caught > 0
    assert res.audit["n_violations"] == probe.violations_caught
    assert probe.max_minute_ts > 0


def test_history_warmup_available(data):
    """回放起点之前的历史（warmup）必须可得 —— 否则均线类策略无法运行。"""
    class Warmup(SandboxStrategy):
        def on_day_close(self, day):
            bars = self.ctx.data.history("SZ.000001", "1d", 300)
            assert len(bars) >= 100        # 起点前已有 ~350 个交易日
            self.ctx.log(f"warmup bars={len(bars)}")
    res = SandboxRunner(_cfg(start="2023-06-01", end="2023-06-05"), [Warmup()], data=data).run()
    assert res.days == 3 or res.days >= 1
