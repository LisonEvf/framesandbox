"""隔离层：虚拟时间补丁 / 恢复 / 断网 / 数据入口劫持。"""

import datetime as dt
import socket
import time

import pytest

from framesandbox import Isolation, SandboxConfig, SandboxRunner, SandboxStrategy
from framesandbox.clock import VirtualClock, dt_to_us, ord_to_dt
from framesandbox.errors import IsolationBreach


class _View:
    """最小网关替身（隔离层只需要 _panel_view / index_series / _last_completed_ord）。"""
    def __init__(self, clock):
        self.clock = clock
    def _panel_view(self):
        import pandas as pd
        return pd.DataFrame({"date": [20750], "symbol": ["X"], "close": [1.0]})
    def index_series(self):
        import pandas as pd
        return pd.Series([3000.0], index=pd.DatetimeIndex(["2026-10-20"]))
    def _last_completed_ord(self):
        return 20750


def test_time_patch_and_restore():
    clock = VirtualClock(dt_to_us(ord_to_dt(20752).replace(hour=10, minute=30)))
    real_now = dt.datetime.now()
    iso = Isolation(clock, _View(clock), block_network=False, patch_data=False)
    with iso:
        v = dt.datetime.now()
        assert v.year == 2026 and v.month == 10 and v.day == 26 or v.year == 2026
        assert abs((v - clock.now_naive()).total_seconds()) < 1
        assert abs(time.time() - clock.now_us() / 1e6) < 1
        assert dt.date.today() == clock.now().date()
        import pandas as pd
        assert pd.Timestamp.now().year == clock.now().year
        # 模块内直接引用也被替换
        import datetime as m
        assert m.datetime.now().hour == 10
    # 恢复
    assert abs((dt.datetime.now() - real_now).total_seconds()) < 5
    assert time.time() > 1_700_000_000


def test_network_block():
    clock = VirtualClock(dt_to_us(ord_to_dt(20752)))
    breaches = []
    iso = Isolation(clock, _View(clock), block_network=True, patch_data=False,
                    breaches=breaches)
    with iso:
        with pytest.raises(IsolationBreach):
            socket.create_connection(("qt.gtimg.cn", 80), timeout=1)
        import urllib.request
        with pytest.raises(IsolationBreach):
            urllib.request.urlopen("http://hq.sinajs.cn/list=sh000001", timeout=1)
    assert len(breaches) == 2 and all(b["blocked"] for b in breaches)
    # 恢复后 socket 可用（不实际联网，只验证补丁已撤）
    assert socket.create_connection.__name__ == "create_connection"


def test_data_entries_hijacked(data):
    """load_panel / load_index 被劫持到 PIT 网关（在真实 runner 内）。"""
    class Check(SandboxStrategy):
        def on_day_close(self, day):
            import _combined_lab as lab
            import market_regime_now as mrn
            p = lab.load_panel()
            assert int(p["date"].max()) <= day["ord"]      # 绝无未来
            s = mrn.load_index()
            from datetime import date as _d
            assert (s.index[-1].date() - _d(1970, 1, 1)).days <= day["ord"]
            self.ctx.log("hijack ok")
    cfg = SandboxConfig(start="2023-06-01", end="2023-06-05", report=False,
                        progress_every=0, patch_data=True)
    res = SandboxRunner(cfg, [Check()], data=data).run()
    assert res.days >= 1


def test_realtime_fetch_disabled(data):
    class Check(SandboxStrategy):
        def on_day_close(self, day):
            import live_signal
            assert live_signal.fetch_sse_realtime() is None
    cfg = SandboxConfig(start="2023-06-01", end="2023-06-02", report=False,
                        progress_every=0)
    SandboxRunner(cfg, [Check()], data=data).run()
