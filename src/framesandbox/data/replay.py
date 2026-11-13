"""回放时间线：把全量历史组织成「逐日剧本」，日内路径按需物化。

结构
----
  - ``ReplayTimeline`` 持有回放区间内每个交易日的 ``ReplayDay``；
  - ``ReplayDay`` 持有当日**权威日线**（全市场）与指数收盘；分钟路径**懒物化**：
    只有被订阅 / 被持仓 / 被检测器关注的标的才展开 240 根分钟 Bar，
    其余标的在日线模式下只经历「开盘 → 收盘」两个事件（长历史回放的性能关键）；
  - 分钟路径优先用真实数仓（``source="real"``），缺失时用确定性合成桥
    （``source="synthetic"``）；对抗注入只作用于合成路径（``source="altered"``）。

因果边界
--------
剧本本身知道整段历史（它是回放引擎），但**策略只能通过网关看到虚拟当前时刻
之前的部分**。日内路径一旦物化，网关也只按分钟索引切片供给，不提前泄露。
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from framebt.core.events import Bar

from ..calendar import SESSION_MINUTES
from .synth import MinutePath, synth_minute_path


def stable_seed(base: int, ord_day: int, symbol: str) -> int:
    """跨平台确定性种子（不用内置 hash —— 它对 str 加盐）。"""
    return zlib.crc32(f"{base}:{ord_day}:{symbol}".encode("utf-8"))


@dataclass
class QuoteState:
    """单标的「截至虚拟现在」的日内行情状态（Runner 逐事件更新）。"""

    symbol: str
    day_ord: int
    prev_close: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    last: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    minute_idx: int = -1          # 已完成的分钟索引（-1 = 今日尚未开盘）
    source: str = "daily"         # daily | real | synthetic | altered
    halted: bool = False
    limit_up: float = 0.0
    limit_down: float = 0.0

    @property
    def vwap(self) -> float:
        return self.amount / self.volume if self.volume > 0 else self.last

    def forming_bar(self) -> Bar:
        """今日正在形成的日线（显式 forming 语义；策略用它=自担假信号风险）。"""
        return Bar(
            timestamp=self.day_ord, symbol=self.symbol,
            open=self.open, high=self.high, low=self.low, close=self.last,
            volume=self.volume, amount=self.amount, vwap=self.vwap,
        )


@dataclass
class ReplayDay:
    """一个交易日的剧本（日线懒加载，内存只保留当前窗口）。"""

    ord: int
    index_close: float | None
    real_intraday: bool
    timeline: "ReplayTimeline"
    _paths: dict[str, MinutePath] = field(default_factory=dict)
    _real_df: object = None
    _real_loaded: bool = False
    _overrides: dict[str, Bar] = field(default_factory=dict)   # 注入改写的日线
    altered: dict[str, str] = field(default_factory=dict)      # symbol → 注入说明

    @property
    def date_str(self) -> str:
        from ..clock import ord_to_date
        return ord_to_date(self.ord).isoformat()

    @property
    def daily(self) -> dict[str, Bar]:
        """当日权威日线（含注入改写）。懒加载 + 引擎侧 LRU。"""
        base = self.timeline.data.daily_bars(self.ord)
        if self._overrides:
            return {**base, **self._overrides}
        return base

    def bar(self, symbol: str) -> Bar | None:
        """单标的当日日线（快路径：symbol_series 缓存 + 注入改写，不建全市场 dict）。"""
        if symbol in self._overrides:
            return self._overrides[symbol]
        import numpy as np
        ser = self.timeline.data.symbol_series(symbol)
        d = ser["date"]
        i = int(np.searchsorted(d, self.ord))
        if i >= len(d) or d[i] != self.ord:
            return None
        return Bar(
            timestamp=self.ord, symbol=symbol,
            open=float(ser["open"][i]), high=float(ser["high"][i]),
            low=float(ser["low"][i]), close=float(ser["close"][i]),
            volume=float(ser["vol"][i]), amount=float(ser["amount"][i]),
            float_shares=float(ser["float_shares"][i]),
            turnover=float(ser["turnover"][i]),
        )

    def prev_close_of(self, symbol: str) -> float | None:
        prev = self.timeline.data.calendar.prev_td(self.ord)
        if prev is None:
            return None
        b = self.timeline.data.daily_bars(prev).get(symbol)
        return b.close if b is not None else None

    @property
    def prev_close(self) -> dict[str, float]:
        """昨日全市场收盘（瞬态构建，不持久缓存 —— 内存友好）。"""
        prev = self.timeline.data.calendar.prev_td(self.ord)
        if prev is None:
            return {}
        return {s: b.close for s, b in self.timeline.data.daily_bars(prev).items()}

    def path(self, symbol: str) -> MinutePath | None:
        """物化（或取缓存）某标的当日分钟路径。无日线 → None（停牌/未上市）。"""
        hit = self._paths.get(symbol)
        if hit is not None:
            return hit
        bar = self.bar(symbol)
        if bar is None:
            return None
        path = self.timeline._build_path(self, symbol, bar)
        self._paths[symbol] = path
        return path

    def alter_high(self, symbol: str, new_high: float, note: str) -> None:
        """对抗注入：同步抬升当日权威日线 high（保持日线/分钟一致）。仅合成日。"""
        bar = self.bar(symbol)
        self._overrides[symbol] = Bar(**{**bar.__dict__, "high": float(new_high)})
        self.altered[symbol] = note


class ReplayTimeline:
    """回放区间 + 懒物化分钟路径 + 可选对抗注入。"""

    def __init__(self, data, *, start_ord: int, end_ord: int, seed: int = 0, injector=None):
        self.data = data
        self.seed = int(seed)
        self.injector = injector
        self.days: list[ReplayDay] = []
        cal = data.calendar
        days = cal.range(start_ord, end_ord)
        if not days:
            raise ValueError(f"回放区间 [{start_ord}, {end_ord}] 内无交易日")
        for d in days:
            self.days.append(ReplayDay(
                ord=d, index_close=data.index_close(d),
                real_intraday=data.has_real_intraday(d), timeline=self,
            ))
        self._day_by_ord = {d.ord: d for d in self.days}

    def day(self, ord_day: int) -> ReplayDay | None:
        return self._day_by_ord.get(int(ord_day))

    def prune_paths(self, current_ord: int) -> None:
        """修剪旧日分钟路径缓存（保留最近 2 日 + 所有被注入改写的日）。"""
        for d in self.days:
            if d.ord >= current_ord - 1 or d.altered:
                continue
            if d._paths:
                d._paths.clear()
                d._real_df = None
                d._real_loaded = False

    # ------------------------------------------------------------------ 路径物化

    def _real_minutes(self, day: ReplayDay):
        if not day._real_loaded:
            day._real_df = self.data.intraday_frame(day.ord) if day.real_intraday else None
            day._real_loaded = True
        return day._real_df

    def _build_path(self, day: ReplayDay, symbol: str, bar: Bar) -> MinutePath:
        df = self._real_minutes(day)
        if df is not None:
            sub = df[df["symbol_key"] == symbol]
            if len(sub) > 0:
                return self._real_path(sub, bar)
        # 合成桥
        path = synth_minute_path(
            bar.open, bar.high, bar.low, bar.close, bar.volume,
            seed=stable_seed(self.seed, day.ord, symbol),
        )
        if self.injector is not None:
            path = self.injector.maybe_alter(day, symbol, bar, path)
        return path

    @staticmethod
    def _real_path(sub, bar: Bar) -> MinutePath:
        """真实分钟 → 240 网格（缺失分钟向前填充，量记 0）。

        **复权口径对齐**：数仓分钟价是不复权原始价，日线是 QFQ。两者在除权除息
        日错位会制造虚假突破/跌破信号。以开盘价为锚（09:30 分钟价 = 开盘集合竞价
        价 = 日线 open）把整条分钟路径缩放到 QFQ 日线口径：factor = bar.open /
        raw_first。成交量本就是未复权原始值，与 QFQ 日线 vol 同口径，不缩放。
        """
        import numpy as np

        m = sub["minute"].to_numpy(dtype=np.int64)
        p = sub["price"].to_numpy(dtype=float)
        v = sub["vol"].to_numpy(dtype=float)
        order = np.argsort(m, kind="stable")
        m, p, v = m[order], p[order], v[order]
        # 去重保最后
        uniq, idx = np.unique(m, return_index=True)
        last_idx = np.append(idx[1:] - 1, len(m) - 1)
        m, p, v = m[last_idx], p[last_idx], v[last_idx]
        lut = {int(mi): (float(pi), float(vi)) for mi, pi, vi in zip(m, p, v)}
        prices, vols = [], []
        last_p = bar.open
        for mi in SESSION_MINUTES:
            if mi in lut:
                last_p, vv = lut[mi]
            else:
                vv = 0.0
            prices.append(last_p)
            vols.append(vv)
        # QFQ 口径对齐（开盘锚点）
        if prices and prices[0] > 0 and bar.open > 0:
            factor = bar.open / prices[0]
            prices = [round(x * factor, 4) for x in prices]
        else:
            prices = [round(x, 4) for x in prices]
        return MinutePath(SESSION_MINUTES, tuple(prices), tuple(vols), "real")
