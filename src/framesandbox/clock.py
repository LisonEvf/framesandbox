"""权威虚拟时钟（沙盒的「现在」）。

实盘策略进入沙盒后，感知到的时间全部来自这里：

  - ``datetime.now()`` / ``time.time()`` / ``pd.Timestamp.now()`` 被隔离层补丁
    重定向到本时钟（见 ``isolation.py``）；
  - 时钟以**整数微秒**（UTC epoch 起）存储，推进只走私有 key 通道（I5 同构：
    公开 API 只读，``set_now`` / ``advance`` 一律 ``ClockWriteViolation``）；
  - 加速倍率 ``speed`` = 每 1 真实秒推进多少虚拟秒。``speed=0`` 表示「尽可能快」
    （事件驱动，不等待真实时间）；``speed>0`` 且 ``pace=True`` 时由 Runner 按
    真实时间节流，供人眼观察「盘中」节奏。

时间口径
--------
  - 虚拟时区固定 Asia/Shanghai（UTC+8，A 股交易所时区，无夏令时）。
  - **逻辑日 = UNIX 天数**（1970-01-01 起算，与面板 ``date`` 列 /
    ``tools._combined_lab.ord_day`` 同约定；注意不同于 framebt 引擎内部的
    ``toordinal()`` —— 沙盒自建 Bar 序列，内部自洽）。
  - 分钟 Bar 时间戳 = ``unix_day * 1_000_000 + hour*60 + minute``
    （与 ``framebt/tools/load_intraday.py`` 同构，跨日严格递增）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .errors import ClockWriteViolation

#: A 股时区（固定 +8，无夏令时）。
SH_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
_EPOCH_DATE = date(1970, 1, 1)
_US = timedelta(microseconds=1)


def dt_to_us(dt: datetime) -> int:
    """datetime → 整数微秒（UTC epoch 起）。naive 视为上海时区。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SH_TZ)
    return (dt - _EPOCH) // _US


def us_to_dt(us: int) -> datetime:
    """整数微秒 → 上海时区 aware datetime。"""
    return (_EPOCH + timedelta(microseconds=int(us))).astimezone(SH_TZ)


def us_to_dt_naive(us: int) -> datetime:
    """整数微秒 → 上海本地 naive datetime（补丁 datetime.now() 用）。"""
    return us_to_dt(us).replace(tzinfo=None)


def day_ord(dt: datetime) -> int:
    """datetime → 逻辑日（UNIX 天数）。"""
    return (dt.date() - _EPOCH_DATE).days


def ord_to_date(ord_day: int) -> date:
    """逻辑日（UNIX 天数）→ date。"""
    return _EPOCH_DATE + timedelta(days=int(ord_day))


def ord_to_dt(ord_day: int) -> datetime:
    """逻辑日 → 该日 00:00 上海时区。"""
    return datetime(ord_to_date(ord_day).year, ord_to_date(ord_day).month,
                    ord_to_date(ord_day).day, tzinfo=SH_TZ)


def iso_to_ord(s: str) -> int:
    """YYYY-MM-DD → 逻辑日。"""
    return (date.fromisoformat(s) - _EPOCH_DATE).days


def minute_ts(ord_day: int, minute_of_day: int) -> int:
    """framebt 兼容的分钟逻辑时间戳。"""
    return ord_day * 1_000_000 + minute_of_day


class VirtualClock:
    """权威虚拟时钟：只读公开面 + 私有推进通道。

    参数：
      start_us：起始虚拟时刻（整数微秒）。
      speed   ：加速倍率（虚拟秒/真实秒）；0 = 事件驱动尽快推进。
    """

    __slots__ = ("_now_us", "_start_us", "_speed", "_key", "_advance_count")

    def __init__(self, start_us: int, speed: float = 0.0):
        self._now_us = int(start_us)
        self._start_us = int(start_us)
        self._speed = float(speed)
        self._key = object()          # 私有推进令牌：只有 Runner 持有
        self._advance_count = 0

    # ---- 只读公开面 ----

    @property
    def key(self) -> object:
        """私有推进令牌。Runner 构造时取走；策略拿到也无法伪造（同一性校验）。"""
        return self._key

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def start_us(self) -> int:
        return self._start_us

    def now_us(self) -> int:
        return self._now_us

    def now(self) -> datetime:
        """当前虚拟时刻（上海时区 aware）。"""
        return us_to_dt(self._now_us)

    def now_naive(self) -> datetime:
        return us_to_dt_naive(self._now_us)

    def today_ord(self) -> int:
        return day_ord(self.now())

    def elapsed_us(self) -> int:
        return self._now_us - self._start_us

    def phase(self) -> str:
        """当前时段标签（不判交易日；日历由 Runner/Calendar 负责）。"""
        m = self.now().hour * 60 + self.now().minute
        if m < 555:
            return "pre_open"
        if m < 570:
            return "auction"        # 09:15–09:29 集合竞价
        if m < 690:
            return "morning"        # 09:30–11:29
        if m < 780:
            return "lunch"          # 11:30–12:59
        if m < 900:
            return "afternoon"      # 13:00–14:59
        return "closed"

    # ---- 私有推进通道（仅 Runner） ----

    def _advance_to(self, us: int, *, _key: object) -> None:
        if _key is not self._key:
            raise ClockWriteViolation("时钟推进令牌无效：只有沙盒 Runner 能推进时间")
        us = int(us)
        if us < self._now_us:
            raise ClockWriteViolation(f"虚拟时间不可回拨：{us} < {self._now_us}")
        self._now_us = us
        self._advance_count += 1

    # ---- 公开诱饵：任何调用都违规（与 framebt I5 同构） ----

    def set_now(self, *a, **k) -> None:
        raise ClockWriteViolation("策略不得篡改权威虚拟时间（沙盒 I5）")

    def advance(self, *a, **k) -> None:
        raise ClockWriteViolation("策略不得推进权威虚拟时间（沙盒 I5）")

    def reset(self, *a, **k) -> None:
        raise ClockWriteViolation("策略不得重置权威虚拟时间（沙盒 I5）")

    def __repr__(self) -> str:  # pragma: no cover
        return f"VirtualClock(now={self.now().isoformat()}, speed={self._speed})"
