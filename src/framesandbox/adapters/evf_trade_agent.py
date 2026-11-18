"""evf-trade-agent 桥接器：一次开发 → 沙盒验证 → 直接实盘。

把 evf-trade-agent 的**策略 + 执行层原样**跑进沙盒：只替换三个 I/O 边界，
策略文件与 config.json 两边共用同一份（见 docs/INTEGRATION_TRADE_AGENT.md）。

  ① PitGatewayShim       实现 trade-agent Gateway 接口（snapshot/kline/quote），
                         底层走 PIT 网关，**不发任何网络请求**；
  ② SandboxBrokerAdapter 实现 BrokerAdapter 契约，place_order → PaperBroker，
                         Executor 以 live 模式运行（成交经真实撮合）；
  ③ EvfReplayDriver      SandboxStrategy 驱动器，虚拟事件替代 Agent 20s 轮询：
                         1m 模式在虚拟 14:50 用 forming 价决策（与实盘同构），
                         每分钟独立止损；daily 模式收盘决策（长历史快速近似）。

复用先例：framebt `OriginFusionAgentStrategy` 已逐字节复用 trade-agent 信号数学；
本模块是同一模式的「实盘时放」一步。trade-agent 侧零改动。

诚实差异（验证结论解读时必读）：
  1. daily 模式决策在 15:00 收盘（实盘 14:50 forming），成交在次日开盘事件
     （限价 crossing）——无盘中假信号暴露，结果偏干净；关键窗口用 1m 复核。
  2. 1m 模式订阅池 = PIT 前 5 日成交额排名（实盘是当日快照排名），近似；
     快照覆盖订阅池，涨跌家数等广度展示字段按池口径（交易链路不受影响）。
  3. 数仓无历史名称：ST 检测失效，可传静态 st_symbols 列表缓解。
  4. 驱动器每日刷新 K 线内存（strategy.warm）。live 进程长期不重启时
     SignalEngine._bar_mem 冻结在启动日（对账发现的 live 潜在问题，
     与 poslayer finite6 同类）——沙盒用正确滚动口径，建议 live 修复。
  5. LLM 门（Agent._refresh_gate）不在验证范围；left_brain 是确定性纯函数，
     随 config 开关。验证与自动交易建议 llm_enabled=false。
  6. Executor 乐观记账 + 每次调仓前 refresh 以 PaperBroker 为准（与实盘
     REST 同构）；撮合拒单经 on_reject 留痕，today_bought 短暂多计 → T+1
     偏保守（安全方向）。
  7. data_dir 强制独立（默认临时目录）：trading_state/signals/kline_cache
     绝不写入 trade-agent 实盘 data/。
  8. 性能旁路：fusion 引擎的磁盘 KlineCache 被替换为实例级内存缓存
     （_MemKlineCache，按虚拟日刷新语义相同）——沙盒运行一次性，跨进程
     持久化无意义，逐日数百标的 JSON 读写是主要剩余开销。仅替换本进程
     实例属性，trade-agent 源码零改动；非 fusion 策略自动回退原缓存。

口径要点：数仓 vol 是「股」，compute_factors 把快照 vol 当「手」×100 ——
Shim 快照行传 vol/100，K 线传原值（与 framebt 先例 L163 一致）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ..clock import ord_to_date
from ..strategy_api import SandboxStrategy

# ---------------------------------------------------------------------------
# trade-agent 引导（与 framebt origin_fusion_agent 同款：环境变量或兄弟目录）
# ---------------------------------------------------------------------------


def _evf_root() -> Path:
    env = os.environ.get("EVF_TRADE_AGENT")
    if env:
        return Path(env).resolve()
    # adapters/evf_trade_agent.py → parents[3] = framesandbox 根 → TRADE/evf-trade-agent
    return Path(__file__).resolve().parents[3].parent / "evf-trade-agent"


_ROOT = _evf_root()
if not _ROOT.is_dir():
    raise ImportError(
        f"找不到 evf-trade-agent 目录：{_ROOT}。请用 EVF_TRADE_AGENT 环境变量指定其路径。")
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.brokers.base import BrokerAdapter, BrokerError  # noqa: E402
from server.executor import Executor  # noqa: E402
from server.store import Store  # noqa: E402
from server.strategies import make_strategy  # noqa: E402

_MARKET_CODE = {"SZ": 0, "SH": 1, "BJ": 2}


def _split(symbol: str) -> tuple[str, str]:
    mk, code = symbol.split(".", 1)
    return mk, code


class _MemKlineCache:
    """内存 K 线缓存：与 trade-agent KlineCache 同语义（按虚拟日刷新），
    返回同格式行 ``[date, open, high, low, close, vol(股), amount]``。

    替换的是**本进程实例属性**（fusion 引擎的 kc），不改 trade-agent 源码；
    沙盒运行一次性，磁盘持久化无意义。shim.kline 不会失败（纯内存），
    故无需原缓存的「网关不可达回退旧盘」分支。
    """

    def __init__(self, days: int):
        self.days = int(days)
        self._mem: dict[str, tuple[str, list]] = {}

    def get(self, gw, symbol: str, today: str, max_age_s: int = 1800) -> list:
        hit = self._mem.get(symbol)
        if hit is not None and hit[0] == today:
            return hit[1]
        rows = [[b["datetime"][:10], b["open"], b["high"], b["low"],
                 b["close"], b["vol"], b["amount"]]
                for b in gw.kline(symbol, self.days)]
        self._mem[symbol] = (today, rows)
        return rows


# ---------------------------------------------------------------------------
# ① 行情边界：PIT 网关 → trade-agent Gateway 接口
# ---------------------------------------------------------------------------
class PitGatewayShim:
    """实现 trade-agent ``Gateway`` 的业务接口，底层全部走 PIT 网关。

    不是网络客户端：没有 urlopen/socket，沙盒隔离层无需为它放行任何地址。
    ``pool`` 由驱动器逐日更新（1m 模式快照覆盖范围）。
    """

    def __init__(self, gateway, *, resolution: str = "daily", pool=(),
                 st_symbols=(), name_map=None):
        self.gw = gateway
        self.resolution = resolution
        self.pool = list(pool)
        self.st_symbols = set(st_symbols)
        self._names = dict(name_map or {})
        self.calls = 0

    def _name(self, symbol: str) -> str:
        if symbol in self.st_symbols:
            return "ST." + _split(symbol)[1]
        return self._names.get(symbol, "")

    def health(self) -> dict:
        return {"ok": True, "source": "framesandbox-pit"}

    def snapshot(self, count: int = 5000) -> list[dict]:
        """全市场快照（trade-agent 口径：按涨幅降序）。

        daily：收盘后由当日截面构造（全市场，含今日已完成日线）；
        1m：由订阅池的当前分钟报价构造（forming 语义，G6）。
        """
        self.calls += 1
        if self.resolution == "1m":
            rows = []
            for q in self.gw.quotes(self.pool).values():
                mk, code = _split(q.symbol)
                rows.append({
                    "market": _MARKET_CODE.get(mk, 0), "code": code,
                    "name": self._name(q.symbol),
                    "close": q.last, "pre_close": q.prev_close,
                    "vol": q.volume / 100.0,          # 股 → 手
                    "amount": q.amount,
                    "open": q.open, "high": q.high, "low": q.low,
                })
        else:
            df = self.gw.panel(n_days=2)
            today = self.gw.today()
            cur = df[df["date"] == today]
            # panel 按 date 升序拼接，两日窗口内 groupby.tail(1) 即昨收，无需排序
            prev = df[df["date"] < today].groupby("symbol").tail(1)
            pre_map = dict(zip(prev["symbol"], prev["close"]))
            rows = []
            for r in cur.itertuples(index=False):
                mk, code = _split(r.symbol)
                rows.append({
                    "market": _MARKET_CODE.get(mk, 0), "code": code,
                    "name": self._name(r.symbol),
                    "close": float(r.close),
                    "pre_close": float(pre_map.get(r.symbol, 0.0)),
                    "vol": float(r.vol) / 100.0,      # 股 → 手
                    "amount": float(r.amount),
                    "open": float(r.open), "high": float(r.high),
                    "low": float(r.low),
                })
        rows.sort(key=lambda r: (r["close"] / r["pre_close"] - 1.0)
                  if r["pre_close"] > 0 else 0.0, reverse=True)
        return rows[:count]

    def kline(self, symbol: str, days: int = 70, adjust: str = "QFQ") -> list[dict]:
        """PIT 日线（QFQ，与实盘 adjust="QFQ" 同口径）。收盘前不含今日。

        vol 传「股」原值（KlineCache 存股；快照 vol 是手，compute_factors
        内 ×100 还原 —— 两者同口径）。
        """
        self.calls += 1
        bars = self.gw.history(symbol, "1d", int(days))
        return [{"datetime": ord_to_date(b.timestamp).isoformat(),
                 "open": b.open, "high": b.high, "low": b.low,
                 "close": b.close, "vol": b.volume, "amount": b.amount}
                for b in bars]

    def quote(self, symbol: str) -> dict:
        self.calls += 1
        q = self.gw.quote(symbol)
        if q is None:
            return {}
        mk, code = _split(symbol)
        return {"market": _MARKET_CODE.get(mk, 0), "code": code,
                "name": self._name(symbol),
                "close": q.last, "pre_close": q.prev_close,
                "vol": q.volume / 100.0, "amount": q.amount,
                "open": q.open, "high": q.high, "low": q.low}

    # 看板专用接口：交易链路不消费，返回空（沙盒无实时异动/分时推送）
    def tick_chart(self, symbol: str, query_date=None) -> list:
        return []

    def unusual(self, market: str = "SH", count: int = 50) -> list:
        return []


# ---------------------------------------------------------------------------
# ② 券商边界：PaperBroker → BrokerAdapter 契约
# ---------------------------------------------------------------------------
class SandboxBrokerAdapter(BrokerAdapter):
    """让 Executor 以 live 模式运行在 PaperBroker 上。

    place_order 返回 = 报单被接受；成交在下一事件（延迟管线），由下一次
    refresh 以 Ledger 为准 —— 与实盘 REST「乐观记账 + 券商纠正」同构。
    报单即拒（不足一手/非法类型等）抛 BrokerError，Executor 记 FAILED。
    """

    def __init__(self, broker, gateway, *, name_map=None):
        self.broker = broker
        self.gw = gateway
        self.url = "sandbox://paperbroker"
        self.calls = 0
        self.last_error = ""
        self._names = dict(name_map or {})

    def health(self) -> bool:
        return True

    def _mark(self, symbol: str, pos) -> float:
        q = self.gw.quote(symbol)
        if q is not None and q.last > 0:
            return q.last
        return pos.avg_price()

    def get_account(self) -> dict:
        self.calls += 1
        marks = {s: self._mark(s, p) for s, p in self.broker.positions.items()}
        eq = self.broker.equity(marks)
        cash = self.broker.cash
        return {"free_amount": cash, "total_asset": eq,
                "stock_market_value": eq - cash}

    def get_positions(self) -> list[dict]:
        self.calls += 1
        today = self.gw.today()
        out = []
        for s, p in self.broker.positions.items():
            mk, code = _split(s)
            hold = p.net_size()
            cost = p.avg_price()
            mark = self._mark(s, p)
            out.append({
                "market": mk, "stock_code": code,
                "stock_name": self._names.get(s, ""),
                "vol_hold": hold,
                "vol_remain": p.available_to_sell(today),   # T+1 可卖
                "cost_price": cost, "cost_value": cost * hold,
                "market_value": mark * hold,
                "curr_profit": (mark - cost) * hold,
                "profit_ratio": (mark / cost - 1.0) * 100.0 if cost > 0 else 0.0,
            })
        return out

    def get_orders(self) -> list[dict]:
        return list(self.broker.order_log)

    def place_order(self, symbol: str, price: float, volume: int,
                    side: str, credit: str = "") -> str:
        self.calls += 1
        limit = float(price) if price > 0 else None     # -1 = 市价
        try:
            if side == "B":
                oid = self.broker.buy(symbol, float(volume), limit=limit)
            elif side == "S":
                oid = self.broker.sell(symbol, float(volume), limit=limit)
            else:
                raise BrokerError(f"未知方向 {side!r}（B/S）")
        except BrokerError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            raise BrokerError(str(exc)) from exc
        rec = next((r for r in reversed(self.broker.order_log)
                    if r["id"] == int(oid)), None)
        if rec is not None and rec["state"] == "rejected":
            self.last_error = rec.get("reason", "rejected")
            raise BrokerError(self.last_error)
        return str(int(oid))

    def cancel_order(self, order_id: str) -> bool:
        from framebt.core.events import OrderId
        return self.broker.cancel(OrderId(int(order_id)))


# ---------------------------------------------------------------------------
# ③ 循环边界：虚拟事件驱动 替代 Agent._loop（20s 真实轮询）
# ---------------------------------------------------------------------------
class EvfReplayDriver(SandboxStrategy):
    """把 trade-agent 注册策略 + Executor 原样跑进沙盒。

    参数：
      cfg                ：trade-agent 完整 config（共享引用；strategy.name 选策略）
      resolution         ：须与 SandboxConfig.resolution 一致（daily | 1m）
      data_dir           ：状态隔离目录；None → 每次运行独立临时目录
      st_symbols         ：静态 ST 列表（数仓无历史名称的缓解手段）
      extra_symbols      ：compute 的自选补算；None → config watchlists 并集
      subscribe_extra    ：1m 模式是否把自选也订阅（严格模式，更慢）
      risk_every_minutes ：1m 模式止损检查间隔（分钟）
      pool_size          ：覆盖 config market.pool_size
      rebalance_minute   ：1m 模式调仓时点（minute-of-day，890 = 14:50）
      paper_reference    ：对账参考臂 —— Executor 用 trade-agent 自带纸面
                           成交（即时/无成本），与验证臂同数据同信号，
                           供「沙盒 vs paper」信号/订单对账（P2）
    """

    name = "evf-trade-agent"

    def __init__(self, cfg: dict, *, resolution: str = "daily", data_dir=None,
                 st_symbols=(), extra_symbols=None, subscribe_extra: bool = False,
                 risk_every_minutes: int = 1, pool_size: int | None = None,
                 rebalance_minute: int = 890, paper_reference: bool = False,
                 entry_price_mode: str = "crossing"):
        self.cfg = cfg
        self.resolution = resolution
        self.paper_reference = bool(paper_reference)
        # 成交时点口径（仅 daily 模式生效）：
        #   crossing（默认，与现网回测同口径）：买单成交于 min(次日开盘, 当日收盘)，
        #         卖单成交于 max(次日开盘, 当日收盘) —— 帧内 crossing 的最优价；
        #   open    ：买单/卖单都成交于次日开盘 —— 真实「次日开盘入场」口径，
        #         用于隔离「回测最优价成交 vs 实盘开盘成交」的落差。
        self.entry_price_mode = str(entry_price_mode).lower()
        if self.paper_reference:
            self.name = "evf-paper-reference"
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.st_symbols = set(st_symbols)
        self.subscribe_extra = bool(subscribe_extra)
        self.risk_every = max(1, int(risk_every_minutes))
        self.pool_size = int(pool_size or (cfg.get("market") or {}).get("pool_size", 200))
        self.rebalance_minute = int(rebalance_minute)
        if extra_symbols is None:
            groups = (cfg.get("watchlists") or {}).get("groups") or {}
            extra_symbols = sorted({s for syms in groups.values() for s in syms})
        self.extra_symbols = list(extra_symbols)
        self.stats = {"rebalances": 0, "risk_checks": 0, "fills": 0, "rejects": 0}
        self.decisions: list[dict] = []
        self.final_status: dict = {}

    # ------------------------------------------------------------------ 初始化

    def on_start(self, ctx):
        super().on_start(ctx)
        if self.data_dir is None:
            self.data_dir = Path(tempfile.mkdtemp(prefix="evf_sandbox_"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.shim = PitGatewayShim(ctx.data, resolution=self.resolution,
                                   st_symbols=self.st_symbols)
        # 策略/执行层/持久化全部是 trade-agent 原物，只换 I/O 边界
        self.strategy = make_strategy(self.shim, self.cfg, self.data_dir)
        # 性能旁路：fusion 引擎的磁盘 K 线缓存 → 内存（同语义，见 docstring §8）
        engine = getattr(self.strategy, "_engine", None)
        kc = getattr(engine, "kc", None)
        if kc is not None and hasattr(kc, "days"):
            engine.kc = _MemKlineCache(kc.days)
        self.store = Store(self.data_dir)
        self.adapter = SandboxBrokerAdapter(ctx.broker, ctx.data)
        tcfg = self.cfg.get("trading") or {}
        if self.paper_reference:
            tcfg = dict(tcfg, live=False)     # 参考臂强制纸面（不受 config 影响）
        self.executor = Executor(self.adapter, tcfg, self.store,
                                 quote_getter=self._quote_price)
        if not self.paper_reference:
            # 沙盒内的「实盘」= PaperBroker（无真钱）；成交走真实撮合而非无成本纸面
            self.executor.set_live(True, confirm="LIVE")

        self._pool = list(ctx.data.universe(self.pool_size, lookback=5))
        self._pool_set = set(self._pool)
        for s in self._pool:
            ctx.subscribe(s)
        if self.resolution == "1m" and self.subscribe_extra:
            for s in self.extra_symbols:
                ctx.subscribe(s)
        self.shim.pool = self._pool
        self._warm_pool = sorted(set(self._pool) | set(self.extra_symbols))

        self._last_res = None
        self._today_ord = 0
        self._rebalanced = False
        self._cur_min = -1
        self._prev_sym = None
        self._tail = None
        self._cur_day = None
        ctx.log(f"[evf] strategy={self.strategy.name} pool={len(self._pool)} "
                f"data_dir={self.data_dir}")

    def _quote_price(self, symbol: str) -> float:
        return float(self.shim.quote(symbol).get("close") or 0.0)

    def _next_day_open(self, day) -> dict[str, float]:
        """次日（下一交易日）全市场开盘价 {symbol: open}。仅用于 open 成交口径。

        传入的 ``day`` 是 ``on_day_close`` 的入参（dict，含 ``ord``/``date_str``），
        不是带 ``.timeline.data`` 的事件对象。下单定价/盯市需要「次日开盘」——相对
        当日收盘属未来数据，PIT 网关（``self.ctx.data``）按当前时刻钳制取不到，故
        经 ``PitGateway._v``（EngineView）回指 ``SandboxData``（引擎侧全量历史）取
        次日日线开盘。取不到即退回 crossing 口径（返回空 dict）。
        """
        try:
            gw = self.ctx.data
            # PitGateway 是策略侧时间钳制网关（无 daily_bars，且取不到「次日」未来日线）；
            # 经 EngineView 回指 SandboxData（引擎侧全量历史）取次日开盘。
            data = gw._v.data if hasattr(gw, "_v") else gw
            next_ord = data.calendar.next_td(day["ord"])
            if next_ord is None:
                return {}
            bars = data.daily_bars(next_ord)
            return {s: float(b.open) for s, b in bars.items() if b is not None and b.open > 0}
        except Exception:  # noqa: BLE001 - 取不到次日开盘就退回 crossing 口径
            return {}

    # ------------------------------------------------------------------ 每日

    def on_day_open(self, day):
        self._today_ord = day["ord"]
        self._rebalanced = False
        self._cur_min = -1
        self._prev_sym = None
        self._tail = None

        # 池逐日漂移（PIT：已完成日成交额排名）；持仓/在途由 Runner 自动物化
        new_pool = list(self.ctx.data.universe(self.pool_size, lookback=5))
        new_set = set(new_pool)
        for s in new_pool:
            if s not in self._pool_set:
                self.ctx.subscribe(s)
        held = set(self.ctx.broker.positions)
        for s in self._pool:
            if s not in new_set and s not in held:
                self.ctx.data.unsubscribe(s)
        self._pool, self._pool_set = new_pool, new_set
        self.shim.pool = new_pool

        # 每日刷新 K 线内存（修正 live _bar_mem 冻结问题，见模块 docstring §4）
        self._warm_pool = sorted(new_set | set(self.extra_symbols))
        self.strategy.warm(self._warm_pool, self.ctx.data.today_str())

    def on_quote(self, q):
        if self.resolution != "1m":
            return
        m = q.minute_of_day
        if m < 0:
            return
        # Runner 逐标的分发：记录每分钟最后一个标的，其报价到达时
        # 本分钟全部物化标的已更新完毕 → 快照是干净的「整分钟」截面
        if m != self._cur_min:
            self._tail = self._prev_sym
            self._cur_min = m
        self._prev_sym = q.symbol
        if self._tail is None or q.symbol != self._tail:
            return
        if m == self.rebalance_minute and not self._rebalanced:
            self._rebalanced = True
            self._daily_pipeline()
        elif self._last_res is not None and m % self.risk_every == 0:
            self._refresh_closes(self._last_res)
            self.executor.risk_check(self._last_res)
            self.stats["risk_checks"] += 1

    def on_day_close(self, day):
        self._cur_day = day
        if self.resolution == "daily":
            # 近似声明：收盘确认决策（实盘 14:50 forming），成交在次日开盘事件
            self._daily_pipeline()
        elif self._last_res is not None:
            self._refresh_closes(self._last_res)
            self.executor.risk_check(self._last_res)   # 收盘止损（live  ticks 到 15:00）
            self.stats["risk_checks"] += 1

    # ------------------------------------------------------------------ 决策管线

    def _daily_pipeline(self):
        """与 Agent.tick 的行动半段同序：compute → risk_check → due_rebalance。"""
        snap = self.strategy.snapshot()
        res = self.strategy.compute(snap, self.extra_symbols)
        # open 成交口径：把融合榜 close 覆盖成次日开盘价，使下单定价（与盯市）
        # 全部落在次日开盘 —— 真实「开盘入场/离场」，隔离帧内 crossing 最优价偏差。
        if self.resolution == "daily" and self.entry_price_mode == "open" and self._cur_day is not None:
            next_open = self._next_day_open(self._cur_day)
            if next_open:
                for x in res.fused:
                    nx = next_open.get(x["symbol"])
                    if nx:
                        x["close"] = nx
        self._last_res = res
        risk = self.executor.risk_check(res)
        self.stats["risk_checks"] += 1
        rec = {"ord": self._today_ord, "date": self.ctx.data.today_str(),
               "gate_on": bool(res.gate_on), "breadth": round(res.breadth, 4),
               "pool_breadth": round(getattr(res, "pool_breadth", 0.0), 4),
               "market_breadth": round(getattr(res, "market_breadth", 0.0), 4),
               "gate_basis": getattr(res, "gate_basis", ""),
               "n_fused": len(res.fused), "placed": 0, "reason": "not_due",
               "fused": [{"symbol": x["symbol"], "score": x.get("score", 0.0),
                          "close": x["close"]} for x in res.fused],
               "orders": [],
               # 独立风控动作（止损等）：与调仓分开留痕，供时间线复盘
               "risk_orders": [{"side": r.get("side"), "symbol": r.get("symbol"),
                                "volume": r.get("volume"), "price": r.get("price"),
                                "tag": r.get("tag", ""), "status": r.get("status")}
                               for r in (risk.get("orders") or [])]}
        if self.executor.due_for_rebalance():
            out = self.executor.rebalance(res)
            self.stats["rebalances"] += 1
            rec["placed"] = out.get("placed", 0)
            rec["reason"] = out.get("reason", "")
            rec["orders"] = [{"side": r.get("side"), "symbol": r.get("symbol"),
                              "volume": r.get("volume"), "price": r.get("price"),
                              "tag": r.get("tag", ""), "status": r.get("status")}
                             for r in out.get("orders", [])]
            # 人类语言归因（executor 事后生成，只读不决策）——时间线复盘用
            rec["attribution"] = out.get("attribution")
        self.decisions.append(rec)

    def _refresh_closes(self, res):
        """止损需要新鲜价：用当前报价刷新融合榜 close（不重算信号）。"""
        for x in res.fused:
            p = self._quote_price(x["symbol"])
            if p > 0:
                x["close"] = p

    # ------------------------------------------------------------------ 回报

    def on_fill(self, fill):
        self.stats["fills"] += 1

    def on_reject(self, rec):
        self.stats["rejects"] += 1
        self.ctx.log(f"[evf] 拒单 {rec['id']} {rec['symbol']} {rec['side']}: {rec['reason']}")

    def on_stop(self):
        self.final_status = self.executor.status()
        self.ctx.log(f"[evf] 结束：{self.stats} · 决策 {len(self.decisions)} 天 "
                     f"· data_dir={self.data_dir}")
