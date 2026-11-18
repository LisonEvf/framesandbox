"""evf-trade-agent 桥接测试：口径换算 / PIT / T+1 / 适配器契约 / 端到端确定性。

依赖兄弟项目 evf-trade-agent（EVF_TRADE_AGENT 环境变量可覆盖路径）。
"""

import json

import pytest

from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy
from framesandbox.adapters.evf_trade_agent import (
    EvfReplayDriver, PitGatewayShim, SandboxBrokerAdapter, _ROOT,
)
from server.brokers.base import BrokerError
from server.signals import compute_factors


def _evf_cfg(**over):
    cfg = {
        "market": {"pool_size": 6, "kline_days": 70},
        "strategy": {"name": "fusion"},
        "signals": {"weights": {"R": 0.4, "S": 0.3, "B": 0.2, "L": 0.1},
                    "top_k": 6, "mom_window": 20, "regime_window": 20,
                    "relvol_window": 5, "relvol_min": 1.5, "max_extreme_mom": 0.5,
                    "gate_breadth": 0.5, "cluster_min_corr": 0.6,
                    "cluster_min_members": 2, "driven_min_limit_up": 2},
        "trading": {"live": False, "paper_cash": 1_000_000, "alloc_pct": 0.9,
                    "max_weight": 0.15, "min_names": 2, "topk": 3,
                    "stop_loss": 0.08, "dd_breaker": 0.15, "dd_cooldown": 80,
                    "rebalance_min_gap_s": 120, "order_price_mode": "limit"},
        "agent": {"llm_enabled": False},
        "watchlists": {"groups": {}},
    }
    for k, v in over.items():
        cfg[k] = v
    return cfg


def _cfg(**kw):
    base = dict(start="2023-06-01", end="2023-09-28", resolution="daily",
                seed=11, report=False, progress_every=0, initial_cash=1_000_000)
    base.update(kw)
    return SandboxConfig(**base)


# ---------------------------------------------------------------------------
# 口径：vol 股/手换算（relvol5 不得被放大 100 倍）
# ---------------------------------------------------------------------------
def test_relvol5_unit_convention():
    """快照 vol 传「手」= 股/100，K 线传「股」→ relvol5 == 1（平量）。"""
    days = [f"2023-01-{d:02d}" for d in range(2, 29)]
    bars = [[d, 10.0, 10.0, 10.0, 10.0, 1_000_000.0, 1e7] for d in days]
    snap = {"close": 10.0, "pre_close": 10.0, "vol": 1_000_000.0 / 100.0,
            "amount": 1e7, "open": 10.0, "high": 10.0, "low": 10.0}
    cfg = _evf_cfg()["signals"]
    f = compute_factors("SZ.000001", "", bars, snap, days[-1], cfg, live=True)
    assert f.relvol5 == pytest.approx(1.0)
    # 反例：若快照误传「股」，relvol5 被放大 100 倍
    bad = compute_factors("SZ.000001", "", bars, dict(snap, vol=1_000_000.0),
                          days[-1], cfg, live=True)
    assert bad.relvol5 == pytest.approx(100.0)


class _ShimProbe(SandboxStrategy):
    """在真实回放里抓取 Shim 输出，验证 PIT 与字段口径。"""

    def on_start(self, ctx):
        super().on_start(ctx)
        self.shim = PitGatewayShim(ctx.data, resolution="daily")
        self.opens, self.closes = [], []

    def on_day_open(self, day):
        self.opens.append((day["date_str"], self.shim.kline("SZ.000001", 30)))

    def on_day_close(self, day):
        self.closes.append((day["date_str"], self.shim.kline("SZ.000001", 30),
                            self.shim.snapshot()))


def test_shim_pit_and_fields(data):
    from framesandbox.clock import iso_to_ord
    probe = _ShimProbe()
    SandboxRunner(_cfg(end="2023-06-15"), [probe], data=data).run()
    assert probe.opens and probe.closes
    # 盘前/盘中：K 线不含今日（G1）
    for date_str, bars in probe.opens:
        assert bars[-1]["datetime"] < date_str
    # 收盘后：含今日；vol 是「股」原值；快照 vol 是「手」
    panel = data.panel
    for date_str, bars, snap in probe.closes:
        assert bars[-1]["datetime"] == date_str
        row = panel[(panel["symbol"] == "SZ.000001")
                    & (panel["date"] == iso_to_ord(date_str))]
        assert bars[-1]["vol"] == float(row["vol"].iloc[0])
        srow = next(r for r in snap if r["code"] == "000001" and r["market"] == 0)
        assert srow["vol"] == pytest.approx(float(row["vol"].iloc[0]) / 100.0)
        assert srow["close"] == float(row["close"].iloc[0])
        assert srow["pre_close"] > 0
    # 快照按涨幅降序
    for _, _, snap in probe.closes:
        pcts = [(r["close"] / r["pre_close"] - 1) if r["pre_close"] > 0 else 0
                for r in snap]
        assert pcts == sorted(pcts, reverse=True)


# ---------------------------------------------------------------------------
# 券商适配器：契约字段 / 延迟成交 / T+1 / 报单即拒
# ---------------------------------------------------------------------------
class _AdapterProbe(SandboxStrategy):
    def on_start(self, ctx):
        super().on_start(ctx)
        self.adapter = SandboxBrokerAdapter(ctx.broker, ctx.data)
        self.day = 0
        self.events = []
        self.oid = None

    def on_day_open(self, day):
        self.day += 1
        if self.day == 2:
            pos = self.adapter.get_positions()
            self.events.append(("day2_positions", pos))
            self.events.append(("day2_account", self.adapter.get_account()))
        if self.day == 3:
            self.events.append(("day3_positions", self.adapter.get_positions()))

    def on_day_close(self, day):
        if self.day == 1:
            px = self.ctx.data.quote("SZ.000001").last
            self.oid = self.adapter.place_order("SZ.000001", px * 1.05, 100, "B")
            with pytest.raises(BrokerError):
                self.adapter.place_order("SZ.000001", px, 50, "B")   # 不足一手
            with pytest.raises(BrokerError):
                self.adapter.place_order("SZ.000001", px, 100, "X")  # 非法方向


def test_adapter_contract_and_t1(data):
    probe = _AdapterProbe()
    res = SandboxRunner(_cfg(end="2023-06-20"), [probe], data=data).run()
    assert isinstance(probe.oid, str)
    assert len(res.fills) == 1                       # 次日开盘事件成交（延迟管线）
    day2 = dict(probe.events)["day2_positions"]
    assert len(day2) == 1
    p = day2[0]
    # 契约最小字段集
    for k in ("market", "stock_code", "stock_name", "vol_hold", "vol_remain",
              "cost_price", "cost_value", "market_value", "curr_profit", "profit_ratio"):
        assert k in p
    assert p["vol_hold"] == 100
    assert p["vol_remain"] == 0                      # T+1：成交当日不可卖
    day3 = dict(probe.events)["day3_positions"]
    assert day3[0]["vol_remain"] == 100              # 次日可卖
    acct = dict(probe.events)["day2_account"]
    assert acct["free_amount"] == pytest.approx(res.equity_curve[1][2])
    assert acct["total_asset"] >= acct["free_amount"]


# ---------------------------------------------------------------------------
# 驱动器：端到端 / 状态隔离 / 确定性 / 前缀不变性
# ---------------------------------------------------------------------------
def test_driver_daily_smoke_and_isolation(data, tmp_path):
    live_state = _ROOT / "data" / "trading_state.json"
    mtime = live_state.stat().st_mtime if live_state.exists() else None

    dd = tmp_path / "evf_state"
    driver = EvfReplayDriver(_evf_cfg(), data_dir=dd)
    res = SandboxRunner(_cfg(), [driver], data=data).run()

    assert res.days > 0
    assert len(driver.decisions) == res.days
    assert res.audit["n_violations"] == 0
    assert res.isolation_breaches == []
    assert all(e[1] > 0 for e in res.equity_curve)
    # 策略拿到的行情句柄是 Shim，不是真实 Gateway
    assert driver.strategy.gw is driver.shim
    assert isinstance(driver.shim, PitGatewayShim)
    # 状态隔离：全部落在 tmp，实盘 data/ 零写入
    assert driver.store.dir == dd
    assert (dd / "trading_state.json").exists()
    assert (dd / "signals.jsonl").exists()
    if mtime is not None:
        assert live_state.stat().st_mtime == mtime
    assert driver.executor.live is True              # 成交经 PaperBroker 撮合
    assert driver.final_status["mode"] == "live"


def test_driver_determinism(data, tmp_path):
    r1 = SandboxRunner(_cfg(), [EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "a")],
                       data=data).run()
    r2 = SandboxRunner(_cfg(), [EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "b")],
                       data=data).run()
    assert r1.equity_curve == r2.equity_curve
    assert [(f.symbol, f.size, f.price, f.cost) for f in r1.fills] == \
           [(f.symbol, f.size, f.price, f.cost) for f in r2.fills]


def test_driver_prefix_invariance(data, tmp_path):
    short = SandboxRunner(_cfg(end="2023-07-15"),
                          [EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "s")],
                          data=data).run()
    long = SandboxRunner(_cfg(), [EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "l")],
                         data=data).run()
    n = len(short.equity_curve)
    assert long.equity_curve[:n] == short.equity_curve


def _trending_panel(n_syms=120):
    """强趋势 + 放量：breadth=1 门开、R 腿合格 → 必然建仓。

    标的数 >= 100：trade-agent SignalEngine._snap_valid 要求快照至少 100 行，
    否则按「陈旧快照」回退（真实全 A 面板天然满足，合成数据需凑够）。
    """
    import pandas as pd
    from tests.conftest import trading_days
    days = trading_days("2022-01-03", 160)
    rows = []
    for si in range(n_syms):
        sym = f"SZ.{si:06d}" if si % 2 == 0 else f"SH.{600000 + si:06d}"
        px, vol = 10.0 + si % 7, 1_000_000.0 * (1 + si)
        for d in days:
            c = px * 1.01
            rows.append((d, sym, px, px * 1.012, px * 0.995, c,
                         vol, vol * (px + c) / 2, 1e8, 0.01))
            # 量增 20%/日 → relvol5 = 1.2^5/mean(1.2^0..4) ≈ 1.55 ≥ 1.5（R 腿门槛）
            px, vol = c, vol * 1.20
    return pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low",
                                       "close", "vol", "amount", "float_shares",
                                       "turnover"])


def test_driver_entries_on_trending_market(tmp_path):
    from framesandbox.data.warehouse import SandboxData
    from tests.conftest import make_index
    panel = _trending_panel()
    days = sorted(panel["date"].unique().tolist())
    data = SandboxData(panel=panel, index=make_index(days))
    from framesandbox.clock import ord_to_date
    cfg = _cfg(start=ord_to_date(days[-40]).isoformat(),
               end=ord_to_date(days[-1]).isoformat())
    driver = EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "trend")
    res = SandboxRunner(cfg, [driver], data=data).run()
    assert res.audit["n_violations"] == 0
    assert any(d["gate_on"] for d in driver.decisions), "强趋势市场门应开"
    assert len(res.fills) > 0, "门开且 R 腿合格应建仓"
    assert all(f.size % 100 == 0 for f in res.fills if f.side.value == "buy")
    # T+1：任何标的当日买入当日不得卖出
    bought = {}
    for f in res.fills:
        if f.side.value == "buy":
            bought.setdefault(f.symbol, set()).add(f.frame_id)
        else:
            assert f.frame_id not in bought.get(f.symbol, set()), "T+1 被破坏"


def test_driver_1m_pipeline(data, tmp_path):
    driver = EvfReplayDriver(_evf_cfg(), resolution="1m", data_dir=tmp_path / "1m")
    cfg = _cfg(start="2023-06-01", end="2023-06-07", resolution="1m")
    res = SandboxRunner(cfg, [driver], data=data).run()
    assert res.audit["n_violations"] == 0
    assert res.isolation_breaches == []
    assert len(driver.decisions) == res.days          # 每日 14:50 管线各跑一次
    assert driver.stats["rebalances"] == res.days
    assert driver.stats["risk_checks"] > res.days     # 分钟级止损在跑
    assert driver.executor.status()["last_rebalance_date"] == driver.decisions[-1]["date"]


class _FormingProbe(SandboxStrategy):
    """1m 模式：14:50 的 Shim 快照 = 当前分钟 forming 价（G6），K 线不含今日。"""

    def on_start(self, ctx):
        super().on_start(ctx)
        self.shim = PitGatewayShim(ctx.data, resolution="1m",
                                   pool=["SZ.000001", "SH.600000"])
        for s in self.shim.pool:
            ctx.subscribe(s)
        self.records = []

    def on_quote(self, q):
        if q.minute_of_day == 890 and q.symbol == "SZ.000001":
            kline = self.shim.kline(q.symbol, 30)
            mins = self.ctx.data.history(q.symbol, "1m", 240)
            cum = sum(b.volume for b in mins if b.timestamp // 1_000_000 == q.day_ord)
            self.records.append((q.day_ord, q.last, self.shim.quote(q.symbol),
                                 kline, cum))


def test_shim_forming_at_1450(data):
    probe = _FormingProbe()
    res = SandboxRunner(_cfg(start="2023-06-01", end="2023-06-07", resolution="1m"),
                        [probe], data=data).run()
    assert res.audit["n_violations"] == 0
    assert len(probe.records) == res.days
    from framesandbox.clock import ord_to_date
    for day_ord, last, qrow, kline, cum in probe.records:
        assert qrow["close"] == last                  # 快照 = 14:50 最新价
        assert qrow["vol"] == pytest.approx(cum / 100.0)   # 累计量 股→手
        assert kline[-1]["datetime"] < ord_to_date(day_ord).isoformat()  # 今日未收盘
