"""SandboxRunner：沙盒编排器（时钟 → 回放 → 网关 → 经纪 → 策略 → 报告）。

每个虚拟交易日的事件时序：

    09:15  时钟就位（盘前）
    09:30  开盘事件：focus 物化（含 stress 注入）→ 撮合预约单 → on_day_open
    09:31..14:59  （1m 模式）分钟事件：报价更新 → 撮合 → 检测器 → on_quote
    15:00  收盘事件：日线定稿 → 撮合 → 假信号判定 → on_day_close → 权益快照

加速：``speed>0 且 pace=True`` 时按真实时间节流（人眼观察「盘中」）；
否则事件驱动尽快推进（长历史回放）。确定性：同配置逐位复现（合成路径用
稳定种子，事件全序，逻辑不读真实时钟）。
"""

from __future__ import annotations

import time as _real_time
from dataclasses import dataclass, field

from .broker import MarketEvent, PaperBroker, default_cost, poslayer_cost
from .calendar import CLOSE_MINUTE, OPEN_MINUTE, SESSION_MINUTES
from .clock import VirtualClock, dt_to_us, iso_to_ord, minute_ts, ord_to_dt
from .data.replay import QuoteState, ReplayTimeline
from .data.warehouse import SandboxData
from .deception import DEFAULT_SPECS, AdversarialInjector, FakeSignalDetector
from .gateway import EngineView, PitGateway, limit_prices
from .strategy_api import SandboxContext, SandboxStrategy


def _to_ord(x) -> int:
    if isinstance(x, int):
        return x
    return iso_to_ord(str(x))


@dataclass
class SandboxConfig:
    start: str | int = "2024-01-01"
    end: str | int = "2026-10-26"
    resolution: str = "daily"          # daily | 1m
    speed: float = 0.0                 # 虚拟秒/真实秒；0 = 尽快
    pace: bool = False                 # True 且 speed>0 → 按真实时间节流
    seed: int = 0
    initial_cash: float = 1_000_000.0
    cost_preset: str = "default"       # default | poslayer | zero
    delay_events: int = 1
    lot: int = 100
    enforce_limits: bool = True
    focus: list | None = None          # 1m 模式订阅标的；None → 自动流动性前 focus_n
    focus_n: int = 30
    stress: dict | None = None         # AdversarialInjector 参数；None = 纯历史回放
    specs: tuple = DEFAULT_SPECS
    detect: bool = True
    audit_full: bool = False
    block_network: bool = True
    patch_data: bool = True
    panel_path: str | None = None
    index_path: str | None = None
    data_dir: str | None = None
    progress_every: int = 50
    strategy_params: dict = field(default_factory=dict)
    report: bool = True
    out_dir: str = "runs"
    run_name: str | None = None


@dataclass
class SandboxResult:
    config: dict
    run_dir: str = ""
    days: int = 0
    equity_curve: list = field(default_factory=list)     # (ord, equity, cash, n_pos)
    fills: list = field(default_factory=list)
    order_log: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    episodes: list = field(default_factory=list)
    deception: dict = field(default_factory=dict)
    injections: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)
    isolation_breaches: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    logs: list = field(default_factory=list)
    wall_seconds: float = 0.0
    final_equity: float = 0.0
    initial_cash: float = 0.0

    def summary(self) -> dict:
        eq = self.equity_curve
        ret = self.final_equity / self.initial_cash - 1 if self.initial_cash else 0.0
        peak, mdd = 0.0, 0.0
        for _, e, _, _ in eq:
            peak = max(peak, e)
            if peak > 0:
                mdd = max(mdd, (peak - e) / peak)
        return {
            "days": self.days, "final_equity": round(self.final_equity, 2),
            "total_return": round(ret, 6), "max_drawdown": round(mdd, 6),
            "fills": len(self.fills), "rejections": len(self.rejections),
            "deception": self.deception, "audit_calls": self.audit.get("total_calls", 0),
            "lookahead_violations": self.audit.get("n_violations", 0),
            "isolation_breaches": len(self.isolation_breaches),
            "wall_seconds": round(self.wall_seconds, 2),
        }


class SandboxRunner:
    def __init__(self, config: SandboxConfig, strategies: list[SandboxStrategy], *, data=None):
        self.config = config
        self.strategies = list(strategies)
        self._data = data
        self.logs: list[str] = []
        self._event_idx = 0
        self._breaches: list[dict] = []

    # ------------------------------------------------------------------ 工具

    def log(self, msg: str) -> None:
        line = f"[{self.clock.now().strftime('%Y-%m-%d %H:%M')}] {msg}"
        self.logs.append(line)
        if len(self.logs) > 20000:
            del self.logs[:5000]

    def _advance(self, us: int) -> None:
        self.clock._advance_to(us, _key=self.clock.key)

    def _us_at(self, ord_day: int, minute: int) -> int:
        dt = ord_to_dt(ord_day).replace(hour=minute // 60, minute=minute % 60)
        return dt_to_us(dt)

    def _cost(self):
        from framebt.cost import NoOpCost
        p = self.config.cost_preset
        if p == "poslayer":
            return poslayer_cost()
        if p == "zero":
            return NoOpCost()
        return default_cost()

    def _auto_focus(self, data, start_ord: int) -> list[str]:
        """PIT 流动性前 N（用 start 之前的数据选，引擎侧合法）。"""
        import numpy as np
        cal = data.calendar
        prev = cal.prev_td(start_ord) or start_ord
        days = cal.range(cal.nth(max(0, cal.index_of(prev) - 20)), prev)
        tot: dict[str, float] = {}
        for d in days:
            for r in data.daily_cross_section(d).itertuples(index=False):
                tot[r.symbol] = tot.get(r.symbol, 0.0) + float(r.amount)
        ranked = sorted(tot.items(), key=lambda kv: kv[1], reverse=True)
        return [s for s, _ in ranked[: self.config.focus_n]]

    # ------------------------------------------------------------------ 主循环

    def run(self) -> SandboxResult:
        cfg = self.config
        t0 = _real_time.monotonic()
        start_ord, end_ord = _to_ord(cfg.start), _to_ord(cfg.end)

        data = self._data if self._data is not None else SandboxData(
            panel_path=cfg.panel_path, index_path=cfg.index_path, data_dir=cfg.data_dir)
        injector = AdversarialInjector(**cfg.stress) if cfg.stress else None
        timeline = ReplayTimeline(data, start_ord=start_ord, end_ord=end_ord,
                                  seed=cfg.seed, injector=injector)
        self.clock = VirtualClock(self._us_at(timeline.days[0].ord, 555), speed=cfg.speed)
        view = EngineView(self.clock, data, timeline)
        view.today_ord = timeline.days[0].ord      # on_start 时网关即有正确的「今天」
        gateway = PitGateway(view, audit_full=cfg.audit_full)
        broker = PaperBroker(initial_cash=cfg.initial_cash, cost_model=self._cost(),
                             delay_events=cfg.delay_events, lot=cfg.lot,
                             enforce_limits=cfg.enforce_limits)
        broker._clock = self.clock
        detector = FakeSignalDetector(cfg.specs) if cfg.detect else None

        focus = set(cfg.focus) if cfg.focus else set()
        if cfg.resolution == "1m" and not focus:
            focus = set(self._auto_focus(data, start_ord))
        view.focus = focus
        view.on_subscribe = lambda sym: self._sync_subscribe(sym, view, timeline)

        ctx = SandboxContext(gateway, broker, self.clock, self)
        broker.on_fill = lambda f: self._dispatch("on_fill", f)
        self._gateway = gateway
        self._broker = broker
        self._detector = detector
        self._view = view
        self._materialized: dict = {}
        # 拒单分发（策略可覆写 on_reject）：撮合拒单 + 报单拒单都通知
        _orig_fail, _orig_reject = broker._fail, broker._reject

        def _fail_with_notify(rec, reason):
            _orig_fail(rec, reason)
            self._dispatch("on_reject", {"id": int(rec.order.id), "symbol": rec.order.symbol,
                                         "side": rec.order.side.value, "reason": reason})

        def _reject_with_notify(order, reason):
            oid = _orig_reject(order, reason)
            self._dispatch("on_reject", {"id": int(oid), "symbol": order.symbol,
                                         "side": order.side.value, "reason": reason})
            return oid

        broker._fail = _fail_with_notify
        broker._reject = _reject_with_notify

        # ---- 隔离层（时间补丁 / 数据入口劫持 / 断网） ----
        from .isolation import Isolation
        iso = Isolation(self.clock, gateway, block_network=cfg.block_network,
                        patch_data=cfg.patch_data, breaches=self._breaches)

        equity_curve: list = []
        day_close_px: dict = {}
        result = SandboxResult(config=self._config_echo(), initial_cash=cfg.initial_cash)

        with iso:
            for s in self.strategies:
                s.on_start(ctx)
            for di, day in enumerate(timeline.days):
                self._run_day(day, view, gateway, broker, detector, timeline,
                              focus, equity_curve, day_close_px)
                if cfg.progress_every and (di + 1) % cfg.progress_every == 0:
                    eq = equity_curve[-1][1] if equity_curve else cfg.initial_cash
                    print(f"  [{di+1}/{len(timeline.days)}] {day.date_str} "
                          f"equity={eq:,.0f} fills={len(broker.fills)}", flush=True)
                if cfg.pace and cfg.speed > 0:
                    target = self.clock.elapsed_us() / 1e6 / cfg.speed
                    spent = _real_time.monotonic() - t0
                    if target > spent:
                        _real_time.sleep(target - spent)
            for s in self.strategies:
                s.on_stop()

        # ---- 收尾：追单关联 / 汇总 ----
        if detector is not None:
            detector.link_orders(broker.order_log, broker.fills, day_close_px)
        result.days = len(timeline.days)
        result.equity_curve = equity_curve
        result.fills = broker.fills
        result.order_log = broker.order_log
        result.rejections = broker.rejections
        result.episodes = [e.as_dict() for e in detector.episodes] if detector else []
        result.deception = detector.summary() if detector else {}
        result.injections = injector.injections if injector else []
        result.audit = gateway.audit_summary()
        result.isolation_breaches = list(self._breaches)
        result.provenance = data.provenance_summary(start_ord, end_ord)
        result.logs = self.logs
        result.final_equity = equity_curve[-1][1] if equity_curve else cfg.initial_cash
        result.wall_seconds = _real_time.monotonic() - t0

        if cfg.report:
            from .report import write_report
            result.run_dir = write_report(result, cfg.out_dir, cfg.run_name)
        return result

    def _config_echo(self) -> dict:
        c = self.config
        return {
            "start": str(c.start), "end": str(c.end), "resolution": c.resolution,
            "speed": c.speed, "pace": c.pace, "seed": c.seed,
            "initial_cash": c.initial_cash, "cost_preset": c.cost_preset,
            "delay_events": c.delay_events, "lot": c.lot,
            "enforce_limits": c.enforce_limits, "focus_n": c.focus_n,
            "stress": c.stress, "detect": c.detect,
            "block_network": c.block_network, "patch_data": c.patch_data,
        }

    def _dispatch(self, hook: str, *args) -> None:
        for s in self.strategies:
            getattr(s, hook)(*args)

    # ------------------------------------------------------------------ 单日

    def _run_day(self, day, view, gateway, broker, detector, timeline,
                 focus, equity_curve, day_close_px) -> None:
        cfg = self.config
        view.today_ord = day.ord
        view.today_closed = False
        view.opened = False
        self._advance(self._us_at(day.ord, 555))
        timeline.prune_paths(day.ord)

        pc_map = day.prev_close
        held = set(broker.positions)
        pending_syms = {o.symbol for o in broker.pending_orders()}
        watch = set(focus) | held | pending_syms
        if detector is not None:
            detector.watch = watch

        # ---- 盘前：物化 focus 路径（stress 注入在此发生，开盘前定稿） ----
        self._materialized = {}
        for sym in sorted(watch):
            path = day.path(sym)
            if path is not None:
                self._materialized[sym] = path
                up, dn = limit_prices(sym, pc_map.get(sym, 0.0) or day.bar(sym).close)
                view.quotes[sym] = QuoteState(
                    symbol=sym, day_ord=day.ord, prev_close=pc_map.get(sym, 0.0),
                    limit_up=up, limit_down=dn, source=path.source,
                )
        if detector is not None:
            detector.on_day_open(day.ord, sorted(watch), gateway)

        # ---- 09:30 开盘事件 ----
        self._advance(self._us_at(day.ord, OPEN_MINUTE))
        view.opened = True
        event_syms = sorted(watch | pending_syms | held)
        for sym in event_syms:
            bar = day.bar(sym)
            if bar is None:
                continue
            st = view.quotes.get(sym)
            if st is not None:
                st.open = st.high = st.low = st.last = bar.open
                st.minute_idx = 0
            up, dn = limit_prices(sym, pc_map.get(sym, 0.0) or bar.close)
            ev = MarketEvent(symbol=sym, day_ord=day.ord, session="open",
                             minute_of_day=OPEN_MINUTE, price=bar.open,
                             open=bar.open, high=bar.open, low=bar.open, volume=0.0,
                             ts_us=self.clock.now_us(), idx=self._next_idx(),
                             limit_up=up, limit_down=dn)
            self._fills(broker._process(ev))
        self._dispatch("on_day_open", {"ord": day.ord, "date_str": day.date_str})

        # ---- 分钟回放（1m 模式） ----
        if cfg.resolution == "1m":
            for i in range(1, len(SESSION_MINUTES)):
                minute = SESSION_MINUTES[i]
                self._advance(self._us_at(day.ord, minute))
                for sym, path in list(self._materialized.items()):
                    st = view.quotes[sym]
                    p, v = path.prices[i], path.vols[i]
                    st.last = p
                    st.high = max(st.high, p)
                    st.low = min(st.low, p) if st.low > 0 else p
                    st.volume += v
                    st.amount += p * v
                    st.minute_idx = i
                    ev = MarketEvent(symbol=sym, day_ord=day.ord, session="minute",
                                     minute_of_day=minute, price=p, open=p, high=p, low=p,
                                     volume=v, ts_us=self.clock.now_us(), idx=self._next_idx(),
                                     limit_up=st.limit_up, limit_down=st.limit_down)
                    self._fills(broker._process(ev))
                    q = gateway._quote_from_state(st)
                    if detector is not None:
                        detector.on_quote(q, gateway)
                    self._dispatch("on_quote", q)

        # ---- 15:00 收盘事件 ----
        self._advance(self._us_at(day.ord, CLOSE_MINUTE))
        view.today_closed = True
        for sym in sorted(set(event_syms) | set(self._materialized)):
            bar = day.bar(sym)
            if bar is None:
                continue
            st = view.quotes.get(sym)
            if st is not None:
                st.open, st.high, st.low, st.last = bar.open, bar.high, bar.low, bar.close
                st.volume, st.amount = bar.volume, bar.amount
                st.minute_idx = len(SESSION_MINUTES) - 1
            up, dn = limit_prices(sym, pc_map.get(sym, 0.0) or bar.close)
            if cfg.resolution == "daily":
                hi, lo, op = bar.high, bar.low, bar.open
            else:
                hi = lo = op = bar.close
            ev = MarketEvent(symbol=sym, day_ord=day.ord, session="close",
                             minute_of_day=CLOSE_MINUTE, price=bar.close,
                             open=op, high=hi, low=lo, volume=bar.volume,
                             ts_us=self.clock.now_us(), idx=self._next_idx(),
                             limit_up=up, limit_down=dn, day_bar=bar)
            self._fills(broker._process(ev))
            day_close_px[(day.ord, sym)] = bar.close
        if detector is not None:
            detector.on_day_close(day.ord, gateway, day)
        self._dispatch("on_day_close", {"ord": day.ord, "date_str": day.date_str,
                                        "index_close": day.index_close})

        # ---- 权益快照 ----
        marks = {}
        for sym in broker.positions:
            bar = day.bar(sym)
            marks[sym] = bar.close if bar is not None else broker._last_fill_px.get(sym, 0.0)
        eq = broker.equity(marks)
        equity_curve.append((day.ord, eq, broker.cash, len(broker.positions)))

    def _next_idx(self) -> int:
        self._event_idx += 1
        return self._event_idx

    def _fills(self, fills) -> None:
        # on_fill 已由 broker.on_fill 分发；这里留作扩展点
        pass

    def _sync_subscribe(self, sym: str, view, timeline) -> None:
        """盘中订阅：立即物化今日路径并把报价状态同步到当前分钟（不泄露未来）。"""
        day = timeline.day(view.today_ord)
        if day is None or not view.opened:
            return
        path = day.path(sym)
        if path is None:
            return
        bar = day.bar(sym)
        pc = day.prev_close_of(sym) or 0.0
        up, dn = limit_prices(sym, pc or bar.close)
        n = self.clock.now().hour * 60 + self.clock.now().minute
        upto = -1
        for i, m in enumerate(SESSION_MINUTES):
            if m <= n:
                upto = i
            else:
                break
        if view.today_closed:
            upto = len(SESSION_MINUTES) - 1
        st = QuoteState(symbol=sym, day_ord=day.ord, prev_close=pc,
                        limit_up=up, limit_down=dn, source=path.source)
        if upto >= 0:
            st.open = path.prices[0]
            st.last = path.prices[upto]
            st.high = max(path.prices[: upto + 1])
            st.low = min(path.prices[: upto + 1])
            st.volume = sum(path.vols[: upto + 1])
            st.amount = sum(p * v for p, v in zip(path.prices[: upto + 1], path.vols[: upto + 1]))
            st.minute_idx = upto
        if view.today_closed:
            st.open, st.high, st.low, st.last = bar.open, bar.high, bar.low, bar.close
            st.volume, st.amount = bar.volume, bar.amount
        view.quotes[sym] = st
        self._materialized[sym] = path      # 盘中订阅立即进入分钟事件循环
