"""PIT 数据网关：策略在沙盒里**唯一**的数据入口。

硬约束（与 framebt I1/I5 同构，另加沙盒特有的审计与诱饵）：

  G1  任何返回记录的逻辑时间戳 <= 权威虚拟时间（日线：今日收盘事件后才含今日；
      分钟：只供已完成的分钟）。
  G2  显式请求未来（``end``/``as_of`` 晚于现在、负窗口）→ ``LookaheadViolation``
      并写入审计违规清单。
  G3  返回副本 / 冻结对象：策略改不动引擎状态。
  G4  每次调用留痕（api / 参数摘要 / 行数 / 最大时间戳），可统计可断言。
  G5  时钟只读；``set_now`` 等诱饵一律 ``ClockWriteViolation``。
  G6  ``forming()`` 显式提供「今日正在形成的日线」——策略用它做盘中决策是
      **合法但自担假信号风险**的（这正是沙盒要训练的场景），与「已完成历史」
      在 API 上明确分离。

逻辑时间戳口径：日线 = ordinal；分钟 = ordinal*1_000_000 + minute_of_day
（与 framebt / tools.load_intraday 一致）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from framebt.core.events import Bar

from .calendar import CLOSE_MINUTE, SESSION_MINUTES
from .clock import VirtualClock, iso_to_ord, minute_ts, ord_to_date
from .errors import LookaheadViolation, SandboxError

# 分板涨跌停幅度（数仓无 ST 历史名称，ST 不单独处理 —— 诚实边界，见 README）
_LIMIT_BY_PREFIX = (
    ("688", 0.20), ("689", 0.20), ("300", 0.20), ("301", 0.20),
    ("920", 0.30), ("83", 0.30), ("87", 0.30), ("43", 0.30), ("82", 0.30), ("88", 0.30),
)


def board_limit(symbol: str) -> float:
    code = symbol.rsplit(".", 1)[-1]
    if symbol.startswith("BJ."):
        return 0.30
    for pre, lim in _LIMIT_BY_PREFIX:
        if code.startswith(pre):
            return lim
    return 0.10


def limit_prices(symbol: str, prev_close: float) -> tuple[float, float]:
    lim = board_limit(symbol)
    return round(prev_close * (1 + lim), 2), round(prev_close * (1 - lim), 2)


@dataclass(frozen=True)
class Quote:
    """截至虚拟现在的行情快照（副本，冻结）。"""

    symbol: str
    ts_us: int
    day_ord: int
    minute_of_day: int        # -1 = 今日尚未开盘
    last: float
    bid: float
    ask: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float             # 今日累计
    amount: float
    vwap: float
    limit_up: float
    limit_down: float
    halted: bool
    source: str               # daily | real | synthetic | altered
    phase: str


class EngineView:
    """Runner 与网关共享的引擎侧视图（策略不可构造/不可写）。"""

    def __init__(self, clock: VirtualClock, data, timeline):
        self.clock = clock
        self.data = data
        self.timeline = timeline
        self.quotes: dict[str, object] = {}      # symbol → QuoteState（仅物化标的）
        self.today_ord: int = 0
        self.today_closed: bool = False
        self.opened: bool = False
        self.focus: set[str] = set()
        self.on_subscribe = None                  # Runner 回调：mid-day 订阅同步


class PitGateway:
    """策略侧数据网关。构造由 Runner 完成；策略只拿到本对象的公开方法。"""

    def __init__(self, view: EngineView, *, audit_full: bool = False):
        self._v = view
        self._audit_full = audit_full
        self.audit_log: list[dict] = []
        self.violations: list[dict] = []
        self.counters: dict[str, int] = {}
        self._panel_cache: dict = {}
        self._universe_cache: dict = {}
        self._max_ts_served = 0

    # ------------------------------------------------------------------ 时间（只读）

    def now(self):
        return self._v.clock.now()

    def now_us(self) -> int:
        return self._v.clock.now_us()

    def today(self) -> int:
        return self._v.today_ord

    def today_str(self) -> str:
        return ord_to_date(self._v.today_ord).isoformat()

    def phase(self) -> str:
        return self._v.clock.phase()

    def is_trading_day(self, ord_day: int | None = None) -> bool:
        return self._v.data.calendar.is_trading_day(self._v.today_ord if ord_day is None else ord_day)

    @property
    def calendar(self):
        """交易日历（只读元数据：next_td / prev_td / count_between）。"""
        return self._v.data.calendar

    def set_now(self, *a, **k):
        raise LookaheadViolation("网关不提供时间写入（沙盒 I5）", api="set_now")

    # ------------------------------------------------------------------ 审计内部

    def _now_minute_ts(self) -> int:
        n = self._v.clock.now()
        m = n.hour * 60 + n.minute
        if self._v.today_closed:
            m = CLOSE_MINUTE
        return minute_ts(self._v.today_ord, min(m, CLOSE_MINUTE))

    def _audit(self, api: str, args: dict, rows: int, max_ts: int) -> None:
        self.counters[api] = self.counters.get(api, 0) + 1
        limit = self._now_minute_ts()
        if max_ts > limit:
            # 内部不变式破坏（引擎 bug）：大声失败
            raise SandboxError(f"G1 破坏：{api} 供出未来数据 max_ts={max_ts} > now={limit}")
        self._max_ts_served = max(self._max_ts_served, max_ts)
        if self._audit_full:
            self.audit_log.append({"us": self._v.clock.now_us(), "api": api,
                                   "args": args, "rows": rows, "max_ts": max_ts})

    def _violation(self, api: str, message: str, requested=None) -> None:
        rec = {"us": self._v.clock.now_us(), "api": api, "message": message,
               "requested": str(requested)}
        self.violations.append(rec)
        raise LookaheadViolation(message, api=api, requested=requested,
                                 now=self._v.clock.now().isoformat())

    # ------------------------------------------------------------------ 行情快照

    def quote(self, symbol: str) -> Quote | None:
        st = self._v.quotes.get(symbol)
        if st is None:
            st = self._fallback_state(symbol)
            if st is None:
                self._audit("quote", {"symbol": symbol}, 0, 0)
                return None
        q = self._quote_from_state(st)
        self._audit("quote", {"symbol": symbol}, 1,
                    minute_ts(st.day_ord, max(q.minute_of_day, 0)))
        return q

    def _quote_from_state(self, st) -> Quote:
        m = st.minute_idx
        mod = SESSION_MINUTES[m] if 0 <= m < len(SESSION_MINUTES) else -1
        tick = 0.01
        bid = max(st.limit_down, round(st.last - tick, 2)) if st.last > 0 else 0.0
        ask = min(st.limit_up, round(st.last + tick, 2)) if st.last > 0 else 0.0
        return Quote(
            symbol=st.symbol, ts_us=self._v.clock.now_us(), day_ord=st.day_ord,
            minute_of_day=mod, last=st.last, bid=bid, ask=ask,
            open=st.open, high=st.high, low=st.low, prev_close=st.prev_close,
            volume=st.volume, amount=st.amount, vwap=st.vwap,
            limit_up=st.limit_up, limit_down=st.limit_down,
            halted=st.halted, source=st.source, phase=self.phase(),
        )

    def _fallback_state(self, symbol: str):
        """非物化标的的日线级快照（未订阅分钟流 → 只有 开盘/收盘 两个已知点）。"""
        from .data.replay import QuoteState
        day = self._v.timeline.day(self._v.today_ord)
        if day is None:
            return None
        bar = day.bar(symbol)
        pc = day.prev_close_of(symbol) or 0.0
        if bar is None:
            # 停牌/未上市：回退到最近一个有成交日的收盘（PIT），标记 halted
            ser = self._v.data.symbol_series(symbol)
            import numpy as np
            d = ser["date"]
            hi = int(np.searchsorted(d, self._v.today_ord, side="right"))
            if hi == 0:
                return None
            last_c = float(ser["close"][hi - 1])
            if last_c <= 0:
                return None
            up, dn = limit_prices(symbol, last_c)
            return QuoteState(symbol=symbol, day_ord=day.ord, prev_close=last_c,
                              last=last_c, halted=True, limit_up=up, limit_down=dn)
        up, dn = limit_prices(symbol, pc if pc > 0 else bar.close)
        src = "altered" if symbol in day.altered else "daily"
        if not self._v.opened:
            return QuoteState(symbol=symbol, day_ord=day.ord, prev_close=pc,
                              last=pc, limit_up=up, limit_down=dn, source=src)
        if not self._v.today_closed:
            return QuoteState(symbol=symbol, day_ord=day.ord, prev_close=pc,
                              open=bar.open, high=bar.open, low=bar.open, last=bar.open,
                              minute_idx=0, limit_up=up, limit_down=dn, source=src)
        return QuoteState(symbol=symbol, day_ord=day.ord, prev_close=pc,
                          open=bar.open, high=bar.high, low=bar.low, last=bar.close,
                          volume=bar.volume, amount=bar.amount, minute_idx=239,
                          limit_up=up, limit_down=dn, source=src)

    def quotes(self, symbols=None) -> dict[str, Quote]:
        syms = list(symbols) if symbols is not None else list(self._v.quotes)
        out = {}
        for s in syms:
            q = self.quote(s)
            if q is not None:
                out[s] = q
        return out

    def forming(self, symbol: str) -> Bar | None:
        """今日正在形成的日线（G6：合法但自担假信号风险）。未开盘返回 None。"""
        st = self._v.quotes.get(symbol)
        if st is None:
            st = self._fallback_state(symbol)
        if st is None or st.minute_idx < 0:
            self._audit("forming", {"symbol": symbol}, 0, 0)
            return None
        b = st.forming_bar()
        m = st.minute_idx
        mod = SESSION_MINUTES[m] if 0 <= m < len(SESSION_MINUTES) else 0
        self._audit("forming", {"symbol": symbol}, 1, minute_ts(st.day_ord, mod))
        return b

    def limits(self, symbol: str) -> tuple[float, float]:
        st = self._v.quotes.get(symbol)
        if st is None:
            day = self._v.timeline.day(self._v.today_ord)
            pc = day.prev_close_of(symbol) if day else None
            if pc is None:
                return (0.0, 0.0)
            return limit_prices(symbol, pc)
        return st.limit_up, st.limit_down

    # ------------------------------------------------------------------ 历史（已完成）

    def _last_completed_ord(self) -> int:
        """最近一个「已完成」的交易日 ordinal（今日收盘后含今日）。"""
        cal = self._v.data.calendar
        if self._v.today_closed:
            return self._v.today_ord
        return cal.prev_td(self._v.today_ord) or self._v.today_ord

    def history(self, symbol: str, period: str = "1d", n: int = 60, *, end=None) -> list[Bar]:
        """已完成 Bar 历史（升序，最多 n 根）。

        period: "1d" | "1m" | "5m"。今日日线仅在收盘事件后可得；分钟只供
        已完成分钟。``end`` 显式晚于现在 → LookaheadViolation。
        """
        if n <= 0:
            self._violation("history", f"非法窗口 n={n}", n)
        if end is not None:
            end_ord = end if isinstance(end, int) else iso_to_ord(str(end))
            if end_ord > self._v.today_ord or (end_ord == self._v.today_ord and not self._v.today_closed):
                self._violation("history", f"end={end} 晚于虚拟现在", end)
        period = period.lower()
        if period == "1d":
            bars = self._daily_history(symbol, n)
        elif period in ("1m", "5m"):
            bars = self._minute_history(symbol, n, agg=5 if period == "5m" else 1)
        else:
            raise ValueError(f"未知 period {period!r}（可选 1d/1m/5m）")
        max_ts = bars[-1].timestamp if bars else 0
        self._audit("history", {"symbol": symbol, "period": period, "n": n}, len(bars), max_ts)
        return bars

    def _daily_history(self, symbol: str, n: int) -> list[Bar]:
        last_ord = self._last_completed_ord()
        ser = self._v.data.symbol_series(symbol)
        import numpy as np
        d = ser["date"]
        hi = int(np.searchsorted(d, last_ord, side="right"))
        lo = max(0, hi - n)
        out = []
        for i in range(lo, hi):
            o = int(d[i])
            # 回放区间内以剧本（含注入改写）为准；区间外取原始面板
            day = self._v.timeline.day(o)
            b = day.bar(symbol) if day is not None else None
            if b is not None:
                out.append(b)
            else:
                out.append(Bar(
                    timestamp=o, symbol=symbol,
                    open=float(ser["open"][i]), high=float(ser["high"][i]),
                    low=float(ser["low"][i]), close=float(ser["close"][i]),
                    volume=float(ser["vol"][i]), amount=float(ser["amount"][i]),
                    float_shares=float(ser["float_shares"][i]),
                    turnover=float(ser["turnover"][i]),
                ))
        return out

    def _minute_history(self, symbol: str, n: int, agg: int = 1) -> list[Bar]:
        """最近 n 根已完成分钟 Bar（跨日回溯；今日只到当前分钟）。"""
        out: list[Bar] = []
        today = self._v.today_ord
        st = self._v.quotes.get(symbol)
        upto = st.minute_idx if (st is not None and st.day_ord == today) else -1
        day = self._v.timeline.day(today)
        if day is not None and upto >= 0:
            path = day.path(symbol)
            if path is not None:
                out.extend(self._path_bars(today, path, upto, symbol))
        # 回溯更早日（仅回放区间内；区间外分钟不提供 —— 诚实边界）
        cal = self._v.data.calendar
        d = cal.prev_td(today)
        while len(out) < n * agg and d is not None:
            pday = self._v.timeline.day(d)
            if pday is None:
                break
            path = pday.path(symbol)
            if path is not None:
                bars = self._path_bars(d, path, len(path) - 1, symbol)
                out = bars + out
            d = cal.prev_td(d)
        out = out[-(n * agg):]
        if agg > 1:
            out = self._aggregate(out, agg)
        return out[-n:]

    @staticmethod
    def _path_bars(ord_day: int, path, upto: int, symbol: str) -> list[Bar]:
        bars = []
        for i in range(upto + 1):
            p = path.prices[i]
            bars.append(Bar(
                timestamp=minute_ts(ord_day, path.minutes[i]), symbol=symbol,
                open=p, high=p, low=p, close=p,
                volume=path.vols[i], amount=path.vols[i] * p, vwap=p,
            ))
        return bars

    @staticmethod
    def _aggregate(bars: list[Bar], k: int) -> list[Bar]:
        out = []
        for i in range(0, len(bars), k):
            chunk = bars[i:i + k]
            if not chunk:
                continue
            out.append(Bar(
                timestamp=chunk[-1].timestamp, symbol=chunk[0].symbol,
                open=chunk[0].open, high=max(b.high for b in chunk),
                low=min(b.low for b in chunk), close=chunk[-1].close,
                volume=sum(b.volume for b in chunk), amount=sum(b.amount for b in chunk),
            ))
        return out

    # ------------------------------------------------------------------ 面板 / 指数

    def panel(self, n_days: int | None = 300, symbols=None, *, full: bool = False, as_of=None):
        """PIT 日线面板副本（列与 live/ 项目面板一致：date/symbol/OHLC/vol/amount/...）。

        只含已完成交易日（今日收盘后含今日）。``n_days`` 截最近 N 日；
        ``full=True`` 忽略 n_days。``as_of`` 晚于现在 → 违规。
        """
        if as_of is not None:
            ao = as_of if isinstance(as_of, int) else iso_to_ord(str(as_of))
            if ao > self._v.today_ord or (ao == self._v.today_ord and not self._v.today_closed):
                self._violation("panel", f"as_of={as_of} 晚于虚拟现在", as_of)
        last_ord = self._last_completed_ord()
        key = (last_ord, None if full else n_days, tuple(symbols) if symbols else None)
        hit = self._panel_cache.get(key)
        if hit is None:
            import pandas as pd
            cal = self._v.data.calendar
            all_days = cal.range(cal.days[0], last_ord)
            days = all_days if (full or n_days is None) else all_days[-n_days:]
            # 按日切片拼接（O(窗口行数)），避免每日扫描全量 15.5M 行面板
            parts = [self._v.data.daily_cross_section(d) for d in days]
            sub = pd.concat(parts, ignore_index=True) if parts else self._v.data.panel.iloc[0:0]
            if symbols is not None:
                sub = sub[sub["symbol"].isin(set(symbols))]
            # 回放区间内的注入改写同步进面板（仅已物化的 altered 日）
            sub = self._apply_altered(sub)
            hit = sub.reset_index(drop=True)
            self._panel_cache.clear()
            self._panel_cache[key] = hit
        out = hit.copy()
        max_ts = int(out["date"].max()) if len(out) else 0
        self._audit("panel", {"n_days": n_days, "full": full}, len(out), max_ts)
        return out

    def _apply_altered(self, sub):
        altered_days = [d for d in self._v.timeline.days if d.altered]
        if not altered_days:
            return sub
        import pandas as pd
        frames = [sub]
        for d in altered_days:
            for sym, _note in d.altered.items():
                bar = d._overrides[sym]
                mask = (sub["date"] == d.ord) & (sub["symbol"] == sym)
                if mask.any():
                    sub = sub.copy()
                    sub.loc[mask, ["open", "high", "low", "close", "vol", "amount"]] = [
                        bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount]
        return sub

    def _panel_view(self):
        """引擎侧无复制 PIT 视图（仅供隔离层补丁 load_panel 使用）。"""
        last_ord = self._last_completed_ord()
        key = ("__view__", last_ord)
        hit = self._panel_cache.get(key)
        if hit is None:
            hit = self._apply_altered(self._v.data.panel[self._v.data.panel["date"] <= last_ord])
            self._panel_cache[key] = hit
        return hit

    def index_series(self):
        """PIT 上证指数收盘序列副本（今日收盘后含今日）。"""
        s = self._v.data.index_series()
        last_ord = self._last_completed_ord()
        cutoff = ord_to_date(last_ord)
        out = s[[t.date() <= cutoff for t in s.index]].copy()
        max_ts = (out.index[-1].date() - date(1970, 1, 1)).days if len(out) else 0
        self._audit("index_series", {}, len(out), max_ts)
        return out

    def universe(self, n: int = 200, lookback: int = 20) -> list[str]:
        """PIT 候选池：最近 lookback 个**已完成**交易日日均成交额前 n。"""
        if n <= 0 or lookback <= 0:
            self._violation("universe", f"非法参数 n={n} lookback={lookback}", (n, lookback))
        last_ord = self._last_completed_ord()
        key = (last_ord, n, lookback)
        hit = self._universe_cache.get(key)
        if hit is None:
            import numpy as np
            cal = self._v.data.calendar
            days = cal.range(cal.prev_td(last_ord) or last_ord, last_ord)[-lookback:]
            tot: dict[str, float] = {}
            cnt: dict[str, int] = {}
            for d in days:
                for r in self._v.data.daily_cross_section(d).itertuples(index=False):
                    tot[r.symbol] = tot.get(r.symbol, 0.0) + float(r.amount)
                    cnt[r.symbol] = cnt.get(r.symbol, 0) + 1
            avg = [(s, tot[s] / cnt[s]) for s in tot if cnt[s] >= max(1, lookback // 2)]
            avg.sort(key=lambda kv: kv[1], reverse=True)
            hit = [s for s, _ in avg[:n]]
            self._universe_cache.clear()
            self._universe_cache[key] = hit
        self._audit("universe", {"n": n, "lookback": lookback}, len(hit), last_ord)
        return list(hit)

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, symbol: str) -> None:
        """订阅标的的日内分钟流（Runner 在下一事件前物化路径）。"""
        self._v.focus.add(symbol)
        if self._v.on_subscribe is not None:
            self._v.on_subscribe(symbol)
        self._audit("subscribe", {"symbol": symbol}, 0, 0)

    def unsubscribe(self, symbol: str) -> None:
        self._v.focus.discard(symbol)
        self._audit("unsubscribe", {"symbol": symbol}, 0, 0)

    def subscribed(self) -> list[str]:
        return sorted(self._v.focus)

    # ------------------------------------------------------------------ 审计汇总

    def audit_summary(self) -> dict:
        return {
            "calls": dict(self.counters),
            "total_calls": sum(self.counters.values()),
            "violations": list(self.violations),
            "n_violations": len(self.violations),
            "max_ts_served": self._max_ts_served,
            "audit_full": self._audit_full,
            "log_rows": len(self.audit_log),
        }
