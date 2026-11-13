"""示例策略：假突破追涨者 vs 收盘确认者（沙盒「欺骗训练」的演示对）。

两个策略交易同一个信号（突破前 20 日高点），区别只在**应对**：

  Chaser      —— 盘中一突破就追买（用 forming 数据）。假突破（冲高回落）会
                 当场骗它进场；T+1 让它当天卖不掉，只能眼睁睁看着回落。
  Disciplined —— 等收盘确认（收盘价仍站在信号位之上）才买，次日开盘成交。
                 躲过绝大多数盘中假突破，代价是入场价更高、信号更少。

跑完对比两份报告：被假信号欺骗次数 / 追假盈亏 / 收益回撤 —— 这就是「提升应对
能力」的量化反馈环。策略代码刻意保持简单，重点在沙盒机制而非 alpha。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from framesandbox import SandboxStrategy   # noqa: E402


class _Base(SandboxStrategy):
    """公共件：20 日高点信号位 + 止损 + 持有 1 日退出。"""

    lookback = 20
    stop_loss = -0.03
    hold_days = 1

    def on_start(self, ctx):
        super().on_start(ctx)
        # 缺省用沙盒 focus 池（自动流动性前 N）；也可 strategy_params={"symbols": [...]}
        self.symbols = list(ctx.params.get("symbols") or ctx.data.subscribed())
        for s in self.symbols:
            ctx.subscribe(s)
        self.level: dict = {}
        self.entry: dict = {}          # sym → (entry_ord, px)
        self.bought_today: set = set()

    def on_day_open(self, day):
        self.bought_today = set()
        self.level = {}
        for s in self.symbols:
            bars = self.ctx.data.history(s, "1d", self.lookback)
            if len(bars) >= self.lookback:
                self.level[s] = max(b.high for b in bars)
        # 持有到期退出（次日开盘卖，T+1 已满足）
        for s, (eord, px) in list(self.entry.items()):
            if self.ctx.data.calendar.count_between(eord, day["ord"]) >= self.hold_days:
                avail = self.ctx.broker.available_to_sell(s, day["ord"])
                if avail > 0:
                    self.ctx.sell(s, avail)

    def _stop_check(self, q):
        e = self.entry.get(q.symbol)
        if e is None:
            return
        if q.last / e[1] - 1 <= self.stop_loss:
            avail = self.ctx.broker.available_to_sell(q.symbol, q.day_ord)
            if avail > 0:
                self.ctx.sell(q.symbol, avail)

    def on_fill(self, fill):
        from framebt.core.events import OrderSide
        if fill.side is OrderSide.BUY:
            self.entry[fill.symbol] = (fill.frame_id, fill.price)
        else:
            self.entry.pop(fill.symbol, None)

    def _budget_buy(self, sym):
        eq = self.ctx.broker.equity({s: self.ctx.data.quote(s).last
                                     for s in self.entry
                                     if self.ctx.data.quote(s) is not None})
        self.ctx.buy(sym, budget=max(10_000.0, eq / 5))


class Chaser(_Base):
    """盘中追突破：一突破就买 —— 假信号的重灾区。"""

    name = "chaser(盘中追突破)"

    def on_quote(self, q):
        self._stop_check(q)
        lv = self.level.get(q.symbol)
        if (lv and q.last > lv and q.symbol not in self.bought_today
                and q.symbol not in self.entry):
            self.bought_today.add(q.symbol)
            self._budget_buy(q.symbol)


class Disciplined(_Base):
    """收盘确认：收盘价仍突破才买，次日开盘成交 —— 用等待换确定性。"""

    name = "disciplined(收盘确认)"

    def on_quote(self, q):
        self._stop_check(q)

    def on_day_close(self, day):
        for s in self.symbols:
            lv = self.level.get(s)
            if lv is None or s in self.entry:
                continue
            bar = self.ctx.data.history(s, "1d", 1)
            if bar and bar[-1].close > lv:
                nxt = self.ctx.data.calendar.next_td(day["ord"])
                if nxt is not None:
                    eq = self.ctx.broker.cash
                    self.ctx.buy(s, budget=max(10_000.0, eq / 5),
                                 activate_ord=nxt, session="open")


class StopRunner(_Base):
    """跌破止损者：首日买入一篮子，盘中跌破 20 日低就止损。

    假跌破（盘中击穿、收盘收回）会把它震在最低点 —— 另一种经典的被假信号
    欺骗。与「不止损持有」对照，量化「止损纪律在假信号环境下的代价」。
    """

    name = "stoprunner(跌破止损)"
    hold_days = 9999

    def on_start(self, ctx):
        super().on_start(ctx)
        self.low_level: dict = {}
        self.stopped: set = set()
        self.entered = False

    def on_day_open(self, day):
        super().on_day_open(day)
        self.low_level = {}
        for s in self.symbols:
            bars = self.ctx.data.history(s, "1d", self.lookback)
            if len(bars) >= self.lookback:
                self.low_level[s] = min(b.low for b in bars)
        if not self.entered:
            self.entered = True
            budget = self.ctx.broker.cash / max(1, len(self.symbols))
            for s in self.symbols:
                self.ctx.buy(s, budget=budget)

    def on_quote(self, q):
        lv = self.low_level.get(q.symbol)
        if (lv and q.last < lv and q.symbol not in self.stopped
                and q.symbol in self.entry):
            avail = self.ctx.broker.available_to_sell(q.symbol, q.day_ord)
            if avail > 0:
                self.stopped.add(q.symbol)
                self.ctx.sell(q.symbol, avail)
