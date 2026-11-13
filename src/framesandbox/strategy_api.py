"""策略接入面：SandboxStrategy 基类 + SandboxContext。

实盘策略项目进沙盒有两种方式：

  1. **适配器**（推荐，见 ``adapters/poslayer.py``）：把项目的逐日决策逻辑移植成
     ``SandboxStrategy`` 回调，数据全部走网关、下单全部走 PaperBroker ——
     策略感知到的是加速的历史时间与 PIT 数据，和实盘同一套反应链路。
  2. **隔离寄生**：项目代码不改，直接 import 进沙盒进程；隔离层把
     ``datetime.now`` / ``load_panel`` / ``load_index`` / 实时行情网络全部劫持到
     虚拟世界（见 ``isolation.py``）。适合「单点快照」型脚本（如 live_now）。

回调时序（每个虚拟交易日）：
    on_day_open → [on_quote × N（1m 模式）] → on_day_close
    on_fill / on_reject 随成交事件插入。
"""

from __future__ import annotations


class SandboxContext:
    """策略上下文：数据（网关）+ 交易（经纪）+ 时钟（只读）+ 日志。"""

    def __init__(self, gateway, broker, clock, runner):
        self.data = gateway
        self.broker = broker
        self.clock = clock
        self._runner = runner
        self.params: dict = dict(runner.config.strategy_params)

    def now(self):
        return self.clock.now()

    def today(self) -> int:
        return self.data.today()

    def log(self, msg: str) -> None:
        self._runner.log(msg)

    def subscribe(self, symbol: str) -> None:
        self.data.subscribe(symbol)

    def buy(self, symbol: str, shares: float = 0.0, **kw):
        return self.broker.buy(symbol, shares, **kw)

    def sell(self, symbol: str, shares: float = 0.0, **kw):
        return self.broker.sell(symbol, shares, **kw)


class SandboxStrategy:
    """沙盒策略基类：覆写关心的回调。``self.ctx`` 在 on_start 注入。"""

    name: str = "strategy"

    def on_start(self, ctx: SandboxContext) -> None:
        self.ctx = ctx

    def on_day_open(self, day: dict) -> None:
        """day = {ord, date_str}。开盘事件后调用（指数收盘此刻未知）。"""

    def on_quote(self, quote) -> None:
        """1m 模式：订阅标的的每个分钟快照。"""

    def on_day_close(self, day: dict) -> None:
        """day = {ord, date_str, index_close}。收盘事件后调用（今日日线已完成）。"""

    def on_fill(self, fill) -> None:
        """成交回报（framebt FillEvent）。"""

    def on_reject(self, rec: dict) -> None:
        """拒单回报。"""

    def on_stop(self) -> None:
        """回放结束。"""
