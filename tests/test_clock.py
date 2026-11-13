"""时钟：写保护 / 单调 / 时段 / 加速映射。"""

import pytest

from framesandbox.clock import VirtualClock, dt_to_us, minute_ts, ord_to_dt, us_to_dt
from framesandbox.errors import ClockWriteViolation


def test_roundtrip():
    dt = ord_to_dt(20752).replace(hour=9, minute=30)
    us = dt_to_us(dt)
    assert us_to_dt(us) == dt
    assert us_to_dt(us).tzinfo.utcoffset(None).total_seconds() == 8 * 3600


def test_write_protection():
    c = VirtualClock(dt_to_us(ord_to_dt(20752)))
    for name in ("set_now", "advance", "reset"):
        with pytest.raises(ClockWriteViolation):
            getattr(c, name)(0)
    with pytest.raises(ClockWriteViolation):
        c._advance_to(c.now_us() + 1, _key=object())   # 伪造令牌
    key = c.key
    c._advance_to(c.now_us() + 60_000_000, _key=key)   # 真令牌可推进


def test_monotonic():
    c = VirtualClock(1_000_000)
    c._advance_to(2_000_000, _key=c.key)
    with pytest.raises(ClockWriteViolation):
        c._advance_to(1_500_000, _key=c.key)           # 不可回拨


def test_phase():
    base = ord_to_dt(20752)
    cases = [(9, 0, "pre_open"), (9, 20, "auction"), (9, 30, "morning"),
             (11, 35, "lunch"), (13, 5, "afternoon"), (15, 0, "closed")]
    for h, m, want in cases:
        c = VirtualClock(dt_to_us(base.replace(hour=h, minute=m)))
        assert c.phase() == want, (h, m)


def test_minute_ts_convention():
    assert minute_ts(20752, 570) == 20752 * 1_000_000 + 570
    assert minute_ts(20752, 900) > minute_ts(20751, 899)
