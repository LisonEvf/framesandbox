"""P2 对账：沙盒验证臂（PaperBroker）vs trade-agent 纸面参考臂。

两臂同数据、同策略、同 config，在一次回放里并行：
  - 信号必须**逐日全等**（compute 只依赖行情，与执行无关）；
  - 首日订单必须全等（同现金、同价格、同目标权重，成交尚未分叉）；
  - 此后订单/权益分叉只能来自执行语义（延迟/成本/涨跌停/现金拒单）。
    权益差异方向不预设：成交时点与每日调仓反馈路径依赖，涨跌市均可能
    朝任一方向（真实数仓对账实证：下跌市沙盒拒单反而保护账户）。
"""

import pytest

from framesandbox import SandboxConfig, SandboxRunner
from framesandbox.adapters.evf_trade_agent import EvfReplayDriver
from tests.test_evf_bridge import _cfg, _evf_cfg, _trending_panel


def _trending_data():
    from framesandbox.data.warehouse import SandboxData
    from tests.conftest import make_index
    panel = _trending_panel()
    days = sorted(panel["date"].unique().tolist())
    return SandboxData(panel=panel, index=make_index(days)), days


def _run_pair(tmp_path, **cfg_kw):
    data, days = _trending_data()
    from framesandbox.clock import ord_to_date
    cfg = _cfg(start=ord_to_date(days[-40]).isoformat(),
               end=ord_to_date(days[-1]).isoformat(), **cfg_kw)
    live = EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "live")
    paper = EvfReplayDriver(_evf_cfg(), data_dir=tmp_path / "paper",
                            paper_reference=True)
    res = SandboxRunner(cfg, [live, paper], data=data).run()
    return res, live, paper


def test_signal_parity_every_day(tmp_path):
    res, live, paper = _run_pair(tmp_path)
    assert res.audit["n_violations"] == 0
    assert res.isolation_breaches == []
    assert live.executor.live is True and paper.executor.live is False
    assert len(live.decisions) == len(paper.decisions) == res.days
    for a, b in zip(live.decisions, paper.decisions):
        assert a["date"] == b["date"]
        assert a["gate_on"] == b["gate_on"]
        assert a["breadth"] == b["breadth"]
        assert a["fused"] == b["fused"], f"{a['date']} 融合榜分叉（信号必须全等）"


def test_day1_order_parity(tmp_path):
    res, live, paper = _run_pair(tmp_path)
    d1_live, d1_paper = live.decisions[0], paper.decisions[0]
    assert d1_live["gate_on"] is True, "趋势市首日门应开（否则无订单可对）"
    key = lambda o: (o["side"], o["symbol"], o["volume"], o["price"])
    assert [key(o) for o in d1_live["orders"]] == [key(o) for o in d1_paper["orders"]]
    assert len(d1_live["orders"]) > 0


def test_execution_divergence_is_visible_and_attributable(tmp_path):
    res, live, paper = _run_pair(tmp_path)
    # 权威权益 = PaperBroker 盯市（executor 内部是乐观记账，拒单也先计持仓）。
    # 权益差异方向不预设：成交时点/成本/拒单通过每日调仓反馈路径依赖，
    # 上涨市与下跌市都可能朝任一方向（真实数仓对账已实证）。
    live_equity = res.equity_curve[-1][1]
    assert live_equity > 0 and paper.final_status["equity"] > 0
    # 受控面板里 open=昨收：次日成交的买单，成交价 ≈ 首日订单价（信号日收盘）。
    # 订单价按 A 股惯例 round 到分，面板价 4 位小数 → 容差 1 分；
    # 差一分钱时限价单可能开盘未 crossing、收盘才成交，故按标的匹配而非顺序。
    first_orders = paper.decisions[0]["orders"]
    assert first_orders, "趋势市首日应有订单"
    order_px = {o["symbol"]: o["price"] for o in first_orders if o["side"] == "B"}
    day2 = live.decisions[1]["ord"]
    day2_buys = [f for f in res.fills if f.side.value == "buy" and f.frame_id == day2]
    assert day2_buys, "次日应有买入成交"
    for f in day2_buys:
        assert f.price == pytest.approx(order_px[f.symbol], abs=0.011)
    # 沙盒臂成交带成本，纸面臂不带
    assert all(f.cost > 0 for f in res.fills)
    # 分叉可解释：后续某日起订单开始不同（成交时点/成本/拒单所致）
    diffs = sum(1 for a, b in zip(live.decisions[1:], paper.decisions[1:])
                if a["orders"] != b["orders"])
    assert diffs > 0, "执行语义差异应在后续订单中可见"


def test_pair_determinism(tmp_path):
    r1, l1, p1 = _run_pair(tmp_path / "r1")
    r2, l2, p2 = _run_pair(tmp_path / "r2")
    assert r1.equity_curve == r2.equity_curve
    assert l1.decisions == l2.decisions
    assert p1.decisions == p2.decisions
