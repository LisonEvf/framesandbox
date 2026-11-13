"""PaperBroker：T+1 / 涨跌停 / 整手 / 成本 / 延迟管线 / 预约单。"""

import pytest

from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy
from framesandbox.broker import PaperBroker, MarketEvent, poslayer_cost
from framesandbox.clock import VirtualClock, dt_to_us, ord_to_dt
from framebt.core.events import OrderSide


def _broker(cash=100_000, **kw):
    b = PaperBroker(initial_cash=cash, **kw)
    b._clock = VirtualClock(dt_to_us(ord_to_dt(20752)))
    return b


def _ev(b, sym="SZ.000001", price=10.0, day=20752, session="minute", idx=1,
        up=11.0, dn=9.0, hi=None, lo=None, op=None):
    return MarketEvent(symbol=sym, day_ord=day, session=session, minute_of_day=571,
                       price=price, open=op if op is not None else price,
                       high=hi if hi is not None else price,
                       low=lo if lo is not None else price,
                       volume=1e6, ts_us=0, idx=idx, limit_up=up, limit_down=dn)


def test_delay_pipeline():
    b = _broker(delay_events=1)
    b._process(_ev(b, idx=1))                        # 推进到事件 1
    b.buy("SZ.000001", 100)                          # 事件 1 期间下单
    assert b._process(_ev(b, idx=1)) == []           # 同事件不成交（I2）
    fills = b._process(_ev(b, idx=2, price=10.2))    # 下一事件成交
    assert len(fills) == 1 and fills[0].price == 10.2
    assert b.cash < 100_000
    # delay=2：隔两个事件才成交
    b2 = _broker(delay_events=2)
    b2._process(_ev(b2, idx=1))
    b2.buy("SZ.000001", 100)
    assert b2._process(_ev(b2, idx=2)) == []
    assert len(b2._process(_ev(b2, idx=3))) == 1


def test_t_plus_1():
    b = _broker(delay_events=0)
    b.buy("SZ.000001", 100)
    b._process(_ev(b, idx=1))
    assert b.position("SZ.000001").net_size() == 100
    b.sell("SZ.000001", 100)
    b._process(_ev(b, idx=2))                        # 当日卖 → T+1 拒
    assert any("T+1" in r["reason"] for r in b.rejections)
    b.sell("SZ.000001", 100)
    fills = b._process(_ev(b, idx=3, day=20753))     # 次日可卖
    assert len(fills) == 1


def test_limit_up_reject_buy():
    b = _broker(delay_events=0)
    b.buy("SZ.000001", 100)
    b._process(_ev(b, idx=1, price=11.0, up=11.0))
    assert any("涨停" in r["reason"] for r in b.rejections)
    assert b.position("SZ.000001").net_size() == 0


def test_limit_down_reject_sell():
    b = _broker(delay_events=0)
    b.buy("SZ.000001", 100)
    b._process(_ev(b, idx=1, price=10.0))
    b.sell("SZ.000001", 100)
    b._process(_ev(b, idx=2, price=9.0, dn=9.0, day=20753))
    assert any("跌停" in r["reason"] for r in b.rejections)


def test_lot_rounding():
    b = _broker(delay_events=0, lot=100)
    b.buy("SZ.000001", 150)                          # → 100
    fills = b._process(_ev(b, idx=1))
    assert fills[0].size == 100
    b2 = _broker(delay_events=0, lot=100)
    b2.buy("SZ.000001", 50)                          # 不足一手 → 拒
    assert any("一手" in r["reason"] for r in b2.rejections)


def test_cash_check():
    b = _broker(cash=1000, delay_events=0)
    b.buy("SZ.000001", 100)                          # 需 ~1002 > 1000
    b._process(_ev(b, idx=1))
    assert any("现金不足" in r["reason"] for r in b.rejections)


def test_limit_order_crossing():
    b = _broker(delay_events=0)
    b.buy("SZ.000001", 100, limit=9.5)
    assert b._process(_ev(b, idx=1, price=10.0, lo=9.8)) == []   # 未触及
    fills = b._process(_ev(b, idx=2, price=9.6, lo=9.4, op=9.9))
    assert len(fills) == 1 and fills[0].price == 9.5             # min(open, limit)


def test_scheduled_close_next():
    b = _broker(delay_events=1)
    b.buy("SZ.000001", 100, activate_ord=20753, session="close")
    assert b._process(_ev(b, idx=1, day=20752, session="close")) == []
    assert b._process(_ev(b, idx=2, day=20753, session="open")) == []
    fills = b._process(_ev(b, idx=3, day=20753, session="close", price=10.5))
    assert len(fills) == 1 and fills[0].price == 10.5


def test_cost_accounting():
    b = _broker(delay_events=0, cost_model=poslayer_cost())
    b.buy("SZ.000001", 1000)
    f = b._process(_ev(b, idx=1, price=10.0))[0]
    assert abs(f.cost - 1000 * 10.0 * 0.0013) < 1e-6
    # 权益独立复算：cash + 持仓×mark
    eq = b.equity({"SZ.000001": 10.0})
    assert abs(eq - (100_000 - f.cost)) < 1e-6
