"""隔离层：让策略「活在历史里」而不自知。

进入沙盒上下文后：

  T1  ``datetime.now() / date.today() / time.time() / pd.Timestamp.now()``
      全部返回**虚拟时间**（扫描 sys.modules 替换直接引用，退出时逐一恢复）。
  T2  实盘项目的数据入口被劫持到 PIT 网关：
      ``_combined_lab.load_panel`` / ``market_regime_now.load_index`` /
      ``OpentdxParquetSource.load``（end 钳制）/ ``load_intraday_bars``（end 钳制）/
      ``live_signal.fetch_sse_realtime``（返回 None，禁止真实指数取数）。
  T3  真实网络取数被阻断（``socket.connect`` / ``urlopen``）→ ``IsolationBreach``
      并留痕。沙盒内策略拿不到「真实现在」的任何信息。

诚实边界（与 framebt I5 相同）：Python 无法阻止策略访问私有属性或 C 层时间
（``np.datetime64('now')`` / ``pd.Timestamp('now')``）。这些属于「越狱」，
测试与审计负责发现，而不是假装能防住。
"""

from __future__ import annotations

import socket
import sys
import time as _time
from datetime import date as _real_date
from datetime import datetime as _real_datetime
from pathlib import Path

from .errors import IsolationBreach

_HERE = Path(__file__).resolve()
_TRADE_ROOT = _HERE.parents[3]          # .../TRADE（isolation.py → framesandbox → src → 项目 → TRADE）
_FRAMEBT = _TRADE_ROOT / "framebt"


class Isolation:
    def __init__(self, clock, gateway, *, block_network=True, patch_data=True,
                 breaches=None, allow_hosts=()):
        self.clock = clock
        self.gateway = gateway
        self.block_network = block_network
        self.patch_data = patch_data
        self.breaches = breaches if breaches is not None else []
        self.allow_hosts = set(allow_hosts)
        self._restore: list[tuple] = []
        self._added_paths: list[str] = []
        self._active = False

    # ------------------------------------------------------------------ 安装/恢复

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    def _set(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def install(self):
        if self._active:
            return
        self._active = True
        self._patch_time()
        if self.patch_data:
            self._patch_data_entries()
        if self.block_network:
            self._patch_network()

    def restore(self):
        if not self._active:
            return
        for obj, attr, orig in reversed(self._restore):
            try:
                setattr(obj, attr, orig)
            except (AttributeError, TypeError):
                pass
        self._restore.clear()
        for p in self._added_paths:
            if p in sys.path:
                sys.path.remove(p)
        self._added_paths.clear()
        self._active = False

    # ------------------------------------------------------------------ T1 时间

    def _patch_time(self):
        import datetime as dt_mod
        clock = self.clock

        class FakeDatetime(_real_datetime):
            @classmethod
            def now(cls, tz=None):
                v = clock.now()
                return v.astimezone(tz) if tz is not None else v.replace(tzinfo=None)

            @classmethod
            def today(cls):
                return clock.now_naive()

            @classmethod
            def utcnow(cls):
                return clock.now().astimezone(dt_mod.timezone.utc).replace(tzinfo=None)

        class FakeDate(_real_date):
            @classmethod
            def today(cls):
                return clock.now().date()

        self._set(dt_mod, "datetime", FakeDatetime)
        self._set(dt_mod, "date", FakeDate)

        # 扫描已加载模块里 `from datetime import datetime/date` 的直接引用
        for name, mod in list(sys.modules.items()):
            if mod is None or name.startswith("framesandbox"):
                continue
            try:
                d = vars(mod)
            except TypeError:
                continue
            for attr, val in list(d.items()):
                if val is _real_datetime:
                    self._restore.append((mod, attr, val))
                    try:
                        setattr(mod, attr, FakeDatetime)
                    except (AttributeError, TypeError):
                        self._restore.pop()
                elif val is _real_date:
                    self._restore.append((mod, attr, val))
                    try:
                        setattr(mod, attr, FakeDate)
                    except (AttributeError, TypeError):
                        self._restore.pop()

        self._set(_time, "time", lambda: clock.now_us() / 1e6)
        self._set(_time, "time_ns", lambda: clock.now_us() * 1000)
        self._set(_time, "localtime", lambda secs=None: _time.localtime(
            clock.now_us() / 1e6 if secs is None else secs))
        self._set(_time, "gmtime", lambda secs=None: _time.gmtime(
            clock.now_us() / 1e6 if secs is None else secs))
        self._set(_time, "ctime", lambda secs=None: _time.ctime(
            clock.now_us() / 1e6 if secs is None else secs))

        try:
            import pandas as pd

            def _ts_now(cls, tz=None):
                v = clock.now()
                ts = pd.Timestamp(v.replace(tzinfo=None))
                return ts.tz_localize(tz) if tz is not None else ts

            self._set(pd.Timestamp, "now", classmethod(_ts_now))
            self._set(pd.Timestamp, "today", classmethod(_ts_now))
            self._set(pd.Timestamp, "utcnow",
                      classmethod(lambda cls: pd.Timestamp(
                          clock.now().astimezone(dt_mod.timezone.utc).replace(tzinfo=None))))
        except ImportError:
            pass

    # ------------------------------------------------------------------ T2 数据入口

    def _add_path(self, p: Path):
        s = str(p)
        if p.is_dir() and s not in sys.path:
            sys.path.insert(0, s)
            self._added_paths.append(s)

    def _patch_data_entries(self):
        gw = self.gateway
        self._add_path(_FRAMEBT / "tools")
        self._add_path(_FRAMEBT / "live")
        self._add_path(_FRAMEBT / "live_now")

        # _combined_lab.load_panel → PIT 视图（引擎侧无复制；live 代码只读不改动它）
        orig_panel = orig_index = None
        try:
            import _combined_lab as lab
            orig_panel = lab.load_panel
            self._set(lab, "load_panel", lambda *a, **k: gw._panel_view())
        except Exception:
            pass
        # market_regime_now.load_index → PIT 指数
        try:
            import market_regime_now as mrn
            orig_index = mrn.load_index
            self._set(mrn, "load_index", lambda: gw.index_series())
        except Exception:
            pass
        # `from market_regime_now import load_index` 的直接引用（live_now/engine.py、
        # live/strategy.py 都是这种导入）也要劫持 —— 扫描已加载模块逐一替换。
        if orig_panel is not None or orig_index is not None:
            new_panel = gw._panel_view
            new_index = gw.index_series
            for name, mod in list(sys.modules.items()):
                if mod is None or name.startswith("framesandbox"):
                    continue
                try:
                    d = vars(mod)
                except TypeError:
                    continue
                for attr, val in list(d.items()):
                    if orig_panel is not None and val is orig_panel:
                        self._restore.append((mod, attr, val))
                        try:
                            setattr(mod, attr, new_panel)
                        except (AttributeError, TypeError):
                            self._restore.pop()
                    elif orig_index is not None and val is orig_index:
                        self._restore.append((mod, attr, val))
                        try:
                            setattr(mod, attr, new_index)
                        except (AttributeError, TypeError):
                            self._restore.pop()
        # live_signal.fetch_sse_realtime → 禁取真实指数（沙盒无指数分钟流，返回 None）
        try:
            import live_signal
            def _no_realtime(timeout=8.0):
                self.breaches.append({"us": self.clock.now_us(),
                                      "target": "fetch_sse_realtime(已劫持为 None)",
                                      "blocked": False})
                return None
            self._set(live_signal, "fetch_sse_realtime", _no_realtime)
        except Exception:
            pass
        # OpentdxParquetSource.load → end 钳制到最近已完成日
        try:
            from framebt.data_sources.opentdx import OpentdxParquetSource
            orig_load = OpentdxParquetSource.load

            def clamped_load(self_src, symbol, start=None, end=None, **kw):
                cutoff = gw._last_completed_ord()
                if end is None:
                    end = cutoff
                else:
                    from .clock import iso_to_ord
                    e = end if isinstance(end, int) else iso_to_ord(str(end))
                    end = min(e, cutoff)
                return orig_load(self_src, symbol, start=start, end=end, **kw)

            self._set(OpentdxParquetSource, "load", clamped_load)
        except Exception:
            pass
        # tools.load_intraday.load_intraday_bars → end_date 钳制
        try:
            import load_intraday as li
            orig_li = li.load_intraday_bars

            def clamped_li(symbols=None, start_date=None, end_date=None):
                cutoff = gw._last_completed_ord()
                from .clock import ord_to_date
                cs = ord_to_date(cutoff).isoformat()
                end_date = min(end_date, cs) if end_date else cs
                return orig_li(symbols=symbols, start_date=start_date, end_date=end_date)

            self._set(li, "load_intraday_bars", clamped_li)
        except Exception:
            pass

    # ------------------------------------------------------------------ T3 网络

    def _patch_network(self):
        breaches = self.breaches
        allow = self.allow_hosts
        clock = self.clock

        def _check(address):
            host = address[0] if isinstance(address, tuple) and address else str(address)
            if host in allow or host in ("127.0.0.1", "::1", "localhost"):
                return
            breaches.append({"us": clock.now_us(), "target": str(address), "blocked": True})
            raise IsolationBreach(
                f"沙盒内禁止真实网络取数：{address}（你感知的时间是历史虚拟时间）",
                target=str(address))

        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex
        orig_create = socket.create_connection

        def connect(sock, address):
            _check(address)
            return orig_connect(sock, address)

        def connect_ex(sock, address):
            _check(address)
            return orig_connect_ex(sock, address)

        def create_connection(address, *a, **k):
            _check(address)
            return orig_create(address, *a, **k)

        self._set(socket.socket, "connect", connect)
        self._set(socket.socket, "connect_ex", connect_ex)
        self._set(socket, "create_connection", create_connection)

        try:
            import urllib.request as ur

            def blocked_urlopen(url, *a, **k):
                target = url.full_url if hasattr(url, "full_url") else str(url)
                breaches.append({"us": clock.now_us(), "target": target, "blocked": True})
                raise IsolationBreach(f"沙盒内禁止真实网络取数：{target}", target=target)

            self._set(ur, "urlopen", blocked_urlopen)
        except ImportError:
            pass
