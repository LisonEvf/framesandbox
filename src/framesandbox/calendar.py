"""交易日历与 A 股交易时段。

日历从数据本身推导（面板/指数的交易日并集），不硬编码节假日——数仓覆盖到哪天，
沙盒就能回放到哪天。时段常量与真实 1 分钟数仓口径一致：

  - 集合竞价 09:15–09:25（可选回放）；
  - 连续竞价分钟网格 = 09:30..11:29 + 13:00..14:59，共 **240** 根
    （与 ``opentdx/db/data/intraday`` 的 240 行/日完全对齐；09:30 首根含开盘
    集合竞价撮合，15:00 收盘集合竞价并入日线收盘事件）。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Iterable

#: 连续竞价分钟（minute-of-day）：09:30..11:29 (120) + 13:00..14:59 (120)。
SESSION_MINUTES: tuple[int, ...] = tuple(range(570, 690)) + tuple(range(780, 900))
#: 集合竞价分钟：09:15..09:25。
AUCTION_MINUTES: tuple[int, ...] = tuple(range(555, 566))
N_MINUTES = len(SESSION_MINUTES)          # 240
OPEN_MINUTE = 570                          # 09:30
CLOSE_MINUTE = 900                         # 15:00（日线收盘事件）


class TradingCalendar:
    """有序交易日（ordinal）日历。"""

    __slots__ = ("_days", "_pos")

    def __init__(self, days: Iterable[int]):
        self._days = sorted({int(d) for d in days})
        self._pos = {d: i for i, d in enumerate(self._days)}

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, ord_day: int) -> bool:
        return int(ord_day) in self._pos

    @property
    def days(self) -> list[int]:
        return list(self._days)

    def is_trading_day(self, ord_day: int) -> bool:
        return int(ord_day) in self._pos

    def index_of(self, ord_day: int) -> int:
        """交易日序号（0 起）；非交易日抛 KeyError。"""
        return self._pos[int(ord_day)]

    def nth(self, i: int) -> int:
        return self._days[i]

    def next_td(self, ord_day: int) -> int | None:
        i = bisect_right(self._days, int(ord_day))
        return self._days[i] if i < len(self._days) else None

    def prev_td(self, ord_day: int) -> int | None:
        i = bisect_left(self._days, int(ord_day)) - 1
        return self._days[i] if i >= 0 else None

    def range(self, start_ord: int, end_ord: int) -> list[int]:
        """[start, end] 闭区间内的交易日。"""
        lo = bisect_left(self._days, int(start_ord))
        hi = bisect_right(self._days, int(end_ord))
        return self._days[lo:hi]

    def count_between(self, a_ord: int, b_ord: int) -> int:
        """(a, b] 之间的交易日数（持有天数用）。"""
        return bisect_right(self._days, int(b_ord)) - bisect_right(self._days, int(a_ord))
