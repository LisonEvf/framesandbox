"""PaperBroker：沙盒内的模拟券商（策略的下单入口）。

复用 framebt 的 ``Ledger``（多腿 / FIFO / T+1）、``StandardCost``（佣金 / 印花税 /
过户费 / 滑点）、``Order / FillEvent`` 事件结构，撮合语义与 framebt 对齐：

  B1  延迟管线：市价单默认在**下一个行情事件**成交（``delay_events=1``，I2 同构）；
      也可 ``delay_events=0` 按当前快照立即成交（更贴近实盘手感，二选一配置）。
  B2  A 股规则：T+1 可卖量、涨停拒买 / 跌停拒卖（分板幅度）、停牌不成交、
      买入 100 股整手（可关）、现金不足拒单、只做多。
  B3  限价单帧内 crossing：买 low<=limit 成交于 min(open, limit)；卖对称。
  B4  成交只使用**当前事件**的价格 —— 经纪商同样看不到未来。
  B5  预约单：``activate_ord``（最早成交日）+ ``session``（open/close 时段），
      用于「次日开盘 / 次日尾盘」这类实盘常见执行安排（poslayer 基线 close_next）。

v1 边界（诚实声明）：整单成交或拒绝，不做部分成交 / 排队 / 盘口深度；
STOP 为触发即市价；IOC/FOK/TRAILING/STOP_LIMIT 未实现（下单即拒并留痕）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from framebt.core.events import Bar, FillEvent, Order, OrderId, OrderSide, OrderState, OrderType
from framebt.core.market_frame import MarketFrame
from framebt.cost import CostModel, StandardCost
from framebt.ledger import Ledger

from .clock import minute_ts
from .errors import SandboxError
from .gateway import limit_prices

_SUPPORTED = (OrderType.MARKET, OrderType.LIMIT, OrderType.STOP)


@dataclass
class MarketEvent:
    """Runner 喂给经纪商的行情事件（只含当前时刻信息）。"""

    symbol: str
    day_ord: int
    session: str            # open | minute | close
    minute_of_day: int
    price: float
    open: float
    high: float
    low: float
    volume: float
    ts_us: int
    idx: int
    limit_up: float = 0.0
    limit_down: float = 0.0
    halted: bool = False
    day_bar: Bar | None = None


@dataclass
class _Rec:
    order: Order
    placed_idx: int
    placed_us: int
    activate_ord: int | None = None
    session: str | None = None
    state: OrderState = OrderState.PENDING


def default_cost() -> StandardCost:
    """默认成本：佣金 3bp(最低 5 元) + 印花税 5bp(卖) + 过户费 0.2bp + 滑点 5bp。"""
    return StandardCost(commission=0.0003, min_commission=5.0, stamp_tax=0.0005,
                        transfer_fee=0.00002, slippage="proportional", k=0.0005)


def poslayer_cost() -> StandardCost:
    """live/ 基线口径：双边各 0.13% 全包（佣金+滑点合并，无印花税单列）。"""
    return StandardCost(commission=0.0013, min_commission=0.0, stamp_tax=0.0,
                        transfer_fee=0.0, slippage="proportional", k=0.0)


class PaperBroker:
    """模拟券商。公开面 = 策略可用的下单/查询 API；``_process`` 仅 Runner 可调。"""

    def __init__(
        self,
        *,
        initial_cash: float = 1_000_000.0,
        cost_model: CostModel | None = None,
        delay_events: int = 1,
        lot: int = 100,
        enforce_limits: bool = True,
        settlement_cycle: int = 1,
    ):
        self.ledger = Ledger(cash=float(initial_cash), settlement_cycle=settlement_cycle)
        self.initial_cash = float(initial_cash)
        self.cost_model = cost_model if cost_model is not None else default_cost()
        self.delay_events = int(delay_events)
        self.lot = int(lot)
        self.enforce_limits = enforce_limits
        self._recs: dict[OrderId, _Rec] = {}
        self._counter = 0
        self._event_idx = 0
        self.fills: list[FillEvent] = []
        self.rejections: list[dict] = []
        self.order_log: list[dict] = []
        self.on_fill = None          # Runner 挂策略回调
        self._clock = None           # Runner 注入（只读）

    # ------------------------------------------------------------------ 策略公开面

    @property
    def cash(self) -> float:
        return self.ledger.cash

    @property
    def positions(self) -> dict:
        return {s: p for s, p in self.ledger._positions.items() if p.net_size() != 0}

    def position(self, symbol: str):
        return self.ledger.position(symbol)

    def available_to_sell(self, symbol: str, day_ord: int) -> float:
        return self.ledger.position(symbol).available_to_sell(day_ord)

    def equity(self, marks: dict[str, float]) -> float:
        return self.ledger.mark_to_market(marks)

    def pending_orders(self) -> list[Order]:
        return [r.order for r in self._recs.values()
                if r.state in (OrderState.PENDING, OrderState.RESTING)]

    def submit(self, order: Order, *, activate_ord: int | None = None,
               session: str | None = None) -> OrderId:
        """报单。返回订单号；不合规直接拒单（留痕）。"""
        if order.order_type not in _SUPPORTED:
            return self._reject(order, f"沙盒 v1 不支持 {order.order_type.value}（仅 MARKET/LIMIT/STOP）")
        if order.side is OrderSide.SELL_SHORT:
            return self._reject(order, "沙盒 v1 只做多（融券未实现）")
        if order.size <= 0 and order.budget <= 0:
            return self._reject(order, "size/budget 必须 > 0")
        if session is not None and session not in ("open", "close"):
            return self._reject(order, f"非法预约时段 {session!r}（open/close）")
        size = order.size
        if size > 0 and self.lot > 0 and order.side is OrderSide.BUY:
            size = float(int(size // self.lot) * self.lot)
            if size < self.lot:
                return self._reject(order, f"买入不足一手（{self.lot} 股）")
            order.size = size
        self._counter += 1
        oid = OrderId(self._counter)
        order.id = oid
        order.state = OrderState.PENDING
        rec = _Rec(order=order, placed_idx=self._event_idx,
                   placed_us=self._clock.now_us() if self._clock else 0,
                   activate_ord=activate_ord, session=session)
        self._recs[oid] = rec
        self.order_log.append({
            "id": int(oid), "symbol": order.symbol, "side": order.side.value,
            "type": order.order_type.value, "size": order.size, "budget": order.budget,
            "price": order.price, "placed_us": rec.placed_us,
            "activate_ord": activate_ord, "session": session, "state": "pending",
        })
        return oid

    def buy(self, symbol: str, shares: float = 0.0, *, budget: float = 0.0,
            limit: float | None = None, activate_ord: int | None = None,
            session: str | None = None) -> OrderId:
        ot = OrderType.LIMIT if limit else OrderType.MARKET
        return self.submit(Order(side=OrderSide.BUY, symbol=symbol, size=float(shares),
                                 budget=float(budget), order_type=ot, price=float(limit or 0.0)),
                           activate_ord=activate_ord, session=session)

    def sell(self, symbol: str, shares: float = 0.0, *, limit: float | None = None,
             activate_ord: int | None = None, session: str | None = None) -> OrderId:
        ot = OrderType.LIMIT if limit else OrderType.MARKET
        return self.submit(Order(side=OrderSide.SELL, symbol=symbol, size=float(shares),
                                 order_type=ot, price=float(limit or 0.0)),
                           activate_ord=activate_ord, session=session)

    def cancel(self, oid: OrderId) -> bool:
        rec = self._recs.get(oid)
        if rec is None or rec.state not in (OrderState.PENDING, OrderState.RESTING):
            return False
        rec.state = OrderState.CANCELLED
        rec.order.state = OrderState.CANCELLED
        self._log_state(oid, "cancelled")
        return True

    def _reject(self, order: Order, reason: str) -> OrderId:
        self._counter += 1
        oid = OrderId(self._counter)
        order.id = oid
        order.state = OrderState.REJECTED
        self.rejections.append({"us": self._clock.now_us() if self._clock else 0,
                                "id": int(oid), "symbol": order.symbol,
                                "side": order.side.value, "reason": reason})
        self.order_log.append({
            "id": int(oid), "symbol": order.symbol, "side": order.side.value,
            "type": order.order_type.value, "size": order.size, "budget": order.budget,
            "price": order.price, "placed_us": self._clock.now_us() if self._clock else 0,
            "activate_ord": None, "session": None, "state": "rejected", "reason": reason,
        })
        return oid

    def _log_state(self, oid: OrderId, state: str, **extra) -> None:
        for rec in reversed(self.order_log):
            if rec["id"] == int(oid):
                rec["state"] = state
                rec.update(extra)
                break

    # ------------------------------------------------------------------ Runner 私有面

    def _process(self, ev: MarketEvent) -> list[FillEvent]:
        """在当前行情事件上撮合所有可成交订单（仅 Runner 调用）。"""
        self._event_idx = ev.idx
        out: list[FillEvent] = []
        for oid, rec in list(self._recs.items()):
            if rec.state not in (OrderState.PENDING, OrderState.RESTING):
                continue
            order = rec.order
            if order.symbol != ev.symbol or ev.halted:
                continue
            if rec.activate_ord is not None and ev.day_ord < rec.activate_ord:
                continue
            if rec.session is not None and ev.session != rec.session:
                continue
            scheduled = rec.activate_ord is not None or rec.session is not None
            if not scheduled and ev.idx - rec.placed_idx < self.delay_events:
                continue
            fill = self._try_fill(rec, ev)
            if fill is not None:
                out.append(fill)
        # 修剪已落地订单（order_log/fills 保留全量历史），避免长回放 O(n²)
        self._recs = {oid: r for oid, r in self._recs.items()
                      if r.state in (OrderState.PENDING, OrderState.RESTING)}
        return out

    def _try_fill(self, rec: _Rec, ev: MarketEvent) -> FillEvent | None:
        order = rec.order
        px = ev.price
        # ---- 涨跌停（用事件价判定；收盘事件用日线收盘） ----
        if self.enforce_limits and ev.limit_up > 0:
            if order.side is OrderSide.BUY and px >= ev.limit_up - 1e-9:
                self._fail(rec, f"涨停拒买（{px:.2f} >= {ev.limit_up:.2f}）")
                return None
            if order.side is OrderSide.SELL and px <= ev.limit_down + 1e-9:
                self._fail(rec, f"跌停拒卖（{px:.2f} <= {ev.limit_down:.2f}）")
                return None
        # ---- 限价 crossing ----
        fill_px = px
        if order.order_type is OrderType.LIMIT:
            if order.side is OrderSide.BUY:
                if ev.low > order.price + 1e-9:
                    rec.state = OrderState.RESTING
                    return None
                fill_px = min(ev.open, order.price)
            else:
                if ev.high < order.price - 1e-9:
                    rec.state = OrderState.RESTING
                    return None
                fill_px = max(ev.open, order.price)
        elif order.order_type is OrderType.STOP:
            if order.side is OrderSide.BUY and ev.high < order.price - 1e-9:
                rec.state = OrderState.RESTING
                return None
            if order.side is OrderSide.SELL and ev.low > order.price + 1e-9:
                rec.state = OrderState.RESTING
                return None
        # ---- 数量（预算单按成交价反算） ----
        size = order.size
        if size <= 0 < order.budget:
            raw = order.budget / (fill_px * 1.002)
            size = float(int(raw // self.lot) * self.lot) if self.lot > 0 else raw
            if size <= 0:
                self._fail(rec, "预算不足一手")
                return None
        # ---- T+1 / 现金 ----
        if order.side is OrderSide.SELL:
            avail = self.ledger.position(order.symbol).available_to_sell(ev.day_ord)
            if avail + 1e-9 < size:
                self._fail(rec, f"T+1：可卖 {avail:.0f} < 下单 {size:.0f}")
                return None
        else:
            need = size * fill_px * 1.002
            if need > self.ledger.cash + 1e-9:
                self._fail(rec, f"现金不足：需 {need:.2f} > 有 {self.ledger.cash:.2f}")
                return None
        # ---- 成交入账 ----
        ts = minute_ts(ev.day_ord, ev.minute_of_day) if ev.session == "minute" else ev.day_ord
        fill = FillEvent(order_id=order.id, side=order.side, symbol=order.symbol,
                         size=float(size), price=float(fill_px),
                         frame_id=ev.day_ord, timestamp=ts)
        frame = MarketFrame(frame_id=ev.day_ord, start_ns=ts, end_ns=ts)
        frame.add_bar(Bar(timestamp=ts, symbol=ev.symbol, open=ev.open, high=ev.high,
                          low=ev.low, close=px, volume=ev.volume))
        fill.cost = float(self.cost_model.cost_for(fill, frame))
        self.ledger.apply_fill(fill)
        rec.state = OrderState.FILLED
        order.state = OrderState.FILLED
        self.fills.append(fill)
        self._log_state(order.id, "filled", fill_px=fill_px, fill_us=ev.ts_us, cost=fill.cost)
        if self.on_fill is not None:
            self.on_fill(fill)
        return fill

    def _fail(self, rec: _Rec, reason: str) -> None:
        rec.state = OrderState.REJECTED
        rec.order.state = OrderState.REJECTED
        self.rejections.append({"us": self._clock.now_us() if self._clock else 0,
                                "id": int(rec.order.id), "symbol": rec.order.symbol,
                                "side": rec.order.side.value, "reason": reason})
        self._log_state(rec.order.id, "rejected", reason=reason)

    def _mark_prices(self, quotes: dict) -> dict[str, float]:
        """持仓标记价（缺报价的持仓用最近成交价兜底并告警）。"""
        marks = {}
        for s, p in self.positions.items():
            q = quotes.get(s)
            if q is not None and q.last > 0:
                marks[s] = q.last
            elif s in self._last_fill_px:
                marks[s] = self._last_fill_px[s]
            else:
                raise SandboxError(f"持仓 {s} 无标记价（数据缺口）")
        return marks

    @property
    def _last_fill_px(self) -> dict:
        out: dict[str, float] = {}
        for f in self.fills:
            out[f.symbol] = f.price
        return out
