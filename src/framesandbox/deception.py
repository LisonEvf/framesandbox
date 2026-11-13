"""假信号检测与对抗注入：沙盒的「欺骗训练」核心。

两个组件
--------
``FakeSignalDetector``
    在回放中实时监测**策略可见口径**的信号（突破 N 日高点 / 跌破 N 日低点 /
    均线金叉），记录每次触发（episode），并在当日收盘时判定：
      - ``held``：收盘仍站在信号位之上 → 真信号；
      - ``fake``：盘中触发、收盘回落 → **假信号**（冲高回落 / 假突破）。
    再把策略在触发后窗口内的同标的下单关联为「追信号」（chased），统计
    「被假信号欺骗」的次数与当日浮亏，供报告与策略迭代。

``AdversarialInjector``（stress 模式，默认关闭）
    在**合成日**主动制造假突破：把价格短暂顶破 N 日高点再回落，并同步改写
    当日日线 high（保持日线/分钟一致），标记 ``source="altered"``。
    **绝不触碰真实数仓日** —— 真实历史里的假信号已经足够多，注入只是加练。

所有判定都只用网关可见数据（已完成历史 + 当前快照），检测器自身不偷看未来。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .data.replay import MinutePath, stable_seed


@dataclass(frozen=True)
class SignalSpec:
    kind: str                  # breakout | breakdown | ma_cross
    n: int = 20                # 突破/跌破回看天数
    fast: int = 5              # ma_cross 快线
    slow: int = 20             # ma_cross 慢线


DEFAULT_SPECS = (SignalSpec("breakout", n=20), SignalSpec("breakdown", n=20))


@dataclass
class SignalEpisode:
    id: int
    symbol: str
    kind: str
    fire_us: int
    fire_day: int
    fire_minute: int
    fire_price: float
    level: float
    outcome: str = "open"      # open | held | fake
    resolve_us: int = 0
    close_price: float = 0.0
    mfe: float = 0.0           # 触发后最大有利偏移（相对触发价）
    mae: float = 0.0           # 触发后最大不利偏移
    chased: bool = False
    chase_order_ids: list = field(default_factory=list)
    chase_pnl_mark: float = 0.0   # 追单按当日收盘标记的盈亏
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "kind": self.kind,
            "fire_us": self.fire_us, "fire_day": self.fire_day,
            "fire_minute": self.fire_minute, "fire_price": self.fire_price,
            "level": self.level, "outcome": self.outcome,
            "resolve_us": self.resolve_us, "close_price": self.close_price,
            "mfe": round(self.mfe, 6), "mae": round(self.mae, 6),
            "chased": self.chased, "chase_order_ids": list(self.chase_order_ids),
            "chase_pnl_mark": round(self.chase_pnl_mark, 2), "source": self.source,
        }


class FakeSignalDetector:
    """盘中信号触发 → 收盘判定 → 追单关联。"""

    def __init__(self, specs=DEFAULT_SPECS, *, chase_window_minutes: int = 240):
        self.specs = tuple(specs)
        self.chase_window_minutes = chase_window_minutes
        self.episodes: list[SignalEpisode] = []
        self._counter = 0
        self._levels: dict = {}          # (day, symbol) → {spec: level}
        self._closes: dict = {}          # (day, symbol) → 已完成收盘序列
        self._fired_today: set = set()   # (day, symbol, kind)
        self._open: dict = {}            # (symbol, kind) → episode（当日未判定）
        self.watch: set[str] = set()

    # ------------------------------------------------------------------ 日内

    def on_day_open(self, day_ord: int, symbols, gateway) -> None:
        """开盘前缓存各标的的信号位（只用已完成历史，日内不变）。"""
        self._fired_today = {k for k in self._fired_today if k[0] != day_ord}
        need = max([s.n for s in self.specs if s.kind in ("breakout", "breakdown")]
                   + [s.slow for s in self.specs if s.kind == "ma_cross"] + [1])
        for sym in symbols:
            bars = gateway.history(sym, "1d", need + 1)
            if not bars:
                continue
            highs = [b.high for b in bars]
            closes = [b.close for b in bars]
            lv = {}
            for spec in self.specs:
                if spec.kind == "breakout" and len(highs) >= spec.n:
                    lv[spec.kind] = max(highs[-spec.n:])
                elif spec.kind == "breakdown" and len(highs) >= spec.n:
                    lv[spec.kind] = min(b.low for b in bars[-spec.n:])
                elif spec.kind == "ma_cross" and len(closes) >= spec.slow:
                    lv[spec.kind] = None      # 均线位逐报价重算
            self._levels[(day_ord, sym)] = lv
            self._closes[(day_ord, sym)] = closes

    def on_quote(self, q, gateway) -> None:
        if q.symbol not in self.watch or q.minute_of_day < 0 or q.last <= 0:
            return
        lv = self._levels.get((q.day_ord, q.symbol))
        if lv is None:
            return
        for spec in self.specs:
            key = (q.day_ord, q.symbol, spec.kind)
            if key in self._fired_today:
                continue
            level = lv.get(spec.kind)
            fired = False
            if spec.kind == "breakout" and level is not None:
                fired = q.last > level
            elif spec.kind == "breakdown" and level is not None:
                fired = q.last < level
            elif spec.kind == "ma_cross":
                closes = self._closes.get((q.day_ord, q.symbol), [])
                if len(closes) >= spec.slow:
                    seq = closes + [q.last]
                    fast = sum(seq[-spec.fast:]) / spec.fast
                    slow = sum(seq[-spec.slow:]) / spec.slow
                    prev_fast = sum(seq[-spec.fast - 1:-1]) / spec.fast
                    prev_slow = sum(seq[-spec.slow - 1:-1]) / spec.slow
                    fired = prev_fast <= prev_slow and fast > slow
                    level = slow
            if fired:
                self._fired_today.add(key)
                self._counter += 1
                ep = SignalEpisode(
                    id=self._counter, symbol=q.symbol, kind=spec.kind,
                    fire_us=q.ts_us, fire_day=q.day_ord, fire_minute=q.minute_of_day,
                    fire_price=q.last, level=float(level or 0.0), source=q.source,
                )
                self.episodes.append(ep)
                self._open[(q.symbol, spec.kind)] = ep
        # 更新未判定 episode 的 MFE/MAE
        for (sym, kind), ep in self._open.items():
            if sym != q.symbol:
                continue
            r = q.last / ep.fire_price - 1 if ep.fire_price > 0 else 0.0
            if kind == "breakdown":
                r = -r
            ep.mfe = max(ep.mfe, r)
            ep.mae = min(ep.mae, r)

    def on_day_close(self, day_ord: int, gateway, day) -> None:
        """收盘判定：信号位是否被收盘确认。``day`` 为 ReplayDay（懒取单标的日线）。"""
        for (sym, kind), ep in list(self._open.items()):
            if ep.fire_day != day_ord:
                continue
            bar = day.bar(sym)
            close = bar.close if bar is not None else 0.0
            ep.close_price = close
            ep.resolve_us = gateway.now_us()
            if kind == "breakout":
                ep.outcome = "held" if close > ep.level else "fake"
            elif kind == "breakdown":
                ep.outcome = "held" if close < ep.level else "fake"
            else:  # ma_cross：收盘仍金叉为 held
                closes = self._closes.get((day_ord, sym), [])
                spec = next((s for s in self.specs if s.kind == "ma_cross"), None)
                if spec and len(closes) >= spec.slow and close > 0:
                    seq = closes + [close]
                    fast = sum(seq[-spec.fast:]) / spec.fast
                    slow = sum(seq[-spec.slow:]) / spec.slow
                    ep.outcome = "held" if fast > slow else "fake"
                else:
                    ep.outcome = "fake"
            del self._open[(sym, kind)]

    # ------------------------------------------------------------------ 追单关联

    def link_orders(self, order_log: list, fills: list, day_close_px: dict) -> None:
        """把触发后窗口内的同标的下单标记为「追信号」，并按当日收盘标记盈亏。

        ``day_close_px``: {(day_ord, symbol): close}。
        """
        fill_by_order = {}
        for f in fills:
            fill_by_order.setdefault(int(f.order_id), []).append(f)
        for ep in self.episodes:
            horizon = ep.fire_us + self.chase_window_minutes * 60 * 1_000_000
            for rec in order_log:
                if rec["symbol"] != ep.symbol or rec.get("placed_us", 0) < ep.fire_us:
                    continue
                if rec["placed_us"] > horizon:
                    continue
                want = "buy" if ep.kind in ("breakout", "ma_cross") else "sell"
                if rec["side"] != want:
                    continue
                ep.chased = True
                ep.chase_order_ids.append(rec["id"])
                close = day_close_px.get((ep.fire_day, ep.symbol), ep.close_price)
                for f in fill_by_order.get(rec["id"], []):
                    sign = 1.0 if f.side.value == "buy" else -1.0
                    ep.chase_pnl_mark += sign * (close - f.price) * f.size

    # ------------------------------------------------------------------ 汇总

    def summary(self) -> dict:
        n = len(self.episodes)
        fake = [e for e in self.episodes if e.outcome == "fake"]
        held = [e for e in self.episodes if e.outcome == "held"]
        chased = [e for e in self.episodes if e.chased]
        chased_fake = [e for e in chased if e.outcome == "fake"]
        by_kind = {}
        for e in self.episodes:
            d = by_kind.setdefault(e.kind, {"fired": 0, "fake": 0, "chased": 0, "chased_fake": 0})
            d["fired"] += 1
            d["fake"] += e.outcome == "fake"
            d["chased"] += e.chased
            d["chased_fake"] += e.chased and e.outcome == "fake"
        return {
            "episodes": n, "held": len(held), "fake": len(fake),
            "fake_rate": len(fake) / n if n else 0.0,
            "chased": len(chased), "chased_fake": len(chased_fake),
            "chase_pnl_mark_total": round(sum(e.chase_pnl_mark for e in chased), 2),
            "chase_pnl_mark_fake": round(sum(e.chase_pnl_mark for e in chased_fake), 2),
            "by_kind": by_kind,
        }


class AdversarialInjector:
    """stress 模式：在合成日制造「假突破加练」。只改合成路径，真实数据不可碰。

    参数：
      prob       ：每个 (合成日, 标的) 注入概率；
      n          ：假突破的目标位 = 前 n 个已完成日最高价；
      spike_pct  ：顶破幅度区间（相对目标位）；
      hold       ：尖峰持续分钟数区间；
      seed       ：确定性种子。
    """

    def __init__(self, *, prob: float = 0.15, n: int = 20,
                 spike_pct: tuple = (0.005, 0.015), hold: tuple = (3, 15), seed: int = 0):
        self.prob = float(prob)
        self.n = int(n)
        self.spike_pct = spike_pct
        self.hold = hold
        self.seed = int(seed)
        self.injections: list[dict] = []

    def maybe_alter(self, day, symbol: str, bar, path: MinutePath) -> MinutePath:
        if path.source != "synthetic":
            return path                      # 真实数据永不改写
        rng = random.Random(stable_seed(self.seed, day.ord, symbol) ^ 0xBEEF)
        if rng.random() > self.prob:
            return path
        ser = day.timeline.data.symbol_series(symbol)
        import numpy as np
        d = ser["date"]
        hi = int(np.searchsorted(d, day.ord, side="left"))
        lo = max(0, hi - self.n)
        if hi - lo < max(5, self.n // 2):
            return path
        level = float(np.max(ser["high"][lo:hi]))
        if level <= 0 or bar.high >= level:
            return path                      # 当日已自然突破，不叠加
        peak = round(level * (1 + rng.uniform(*self.spike_pct)), 2)
        t0 = rng.randint(30, 200)
        k = rng.randint(*self.hold)
        t1 = min(t0 + k, len(path) - 2)
        prices = list(path.prices)
        base = prices[t0]
        half = max(1, (t1 - t0) // 2)
        for i in range(t0, t1 + 1):
            if i <= t0 + half:
                p = base + (peak - base) * (i - t0) / half
            else:
                p = peak + (prices[t1] - peak) * (i - t0 - half) / max(1, t1 - t0 - half)
            prices[i] = round(max(bar.low, min(peak, p)), 4)
        note = f"stress 假突破：{t0}→{t1} 分钟顶破 {self.n} 日高 {level:.2f} 至 {peak:.2f} 后回落"
        day.alter_high(symbol, peak, note)
        self.injections.append({"day": day.ord, "symbol": symbol, "level": level,
                                "peak": peak, "t0": t0, "t1": t1, "note": note})
        return MinutePath(path.minutes, tuple(prices), path.vols, "altered", note)
