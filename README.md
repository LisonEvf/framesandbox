# framesandbox

**基于 [framebt](../framebt/) 的实盘时放沙盒 · Live-fire replay sandbox**

> 让实盘策略项目「活在历史里」：感知到的是**加速的历史时间**与**PIT 历史数据**，
> 盘中假信号会真实地欺骗它，而数据前视被结构性隔绝。被骗多少次、代价多大、
> 怎么应对更好 —— 全部量化留痕，可复现、可迭代。

## 为什么需要沙盒

日线回测里，「假突破」根本不存在 —— 收盘没突破，信号就没出现。但实盘盘中，
价格**真的**冲破过 20 日高点，你的策略**真的**追了进去，然后收盘回落，T+1 让你
当天卖不掉。这种「被假信号欺骗」的体验，传统回测给不了。

framesandbox 把历史按分钟加速回放，策略用和实盘一样的反应链路（行情快照 →
决策 → 报单 → 成交回报）运行，同时：

- **前视隔绝**：策略唯一数据入口是 PIT 网关，一切访问钳制到虚拟现在，
  显式请求未来直接抛 `LookaheadViolation` 并留痕；
- **时间隔离**：`datetime.now()` / `time.time()` / `pd.Timestamp.now()` 全部返回
  虚拟时间；真实行情网络被阻断 —— 策略拿不到「真实现在」的任何信息；
- **欺骗统计**：每次盘中信号触发都被记录，收盘判定真/假，策略的追单被关联，
  盈亏被归因 —— 「应对能力」有了量化反馈环。

## 安装与快速开始

要求 Python 3.12+，与 framebt 同目录布局（`TRADE/framebt`、`TRADE/framesandbox`、
`TRADE/opentdx`）。核心零第三方依赖；读真实数仓需要 `pandas + pyarrow`。

```bash
cd framesandbox
pip install -e ".[all]"        # 或直接把 src 加入 PYTHONPATH（framebt 自动引导）
python -m pytest               # 54 项测试：前视攻击/时钟写保护/T+1/QFQ对齐/口径对账/确定性/端到端/evf桥接/双臂对账
```

### 30 秒：内存数据跑通（无需数仓）

```python
from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy

class MyStrategy(SandboxStrategy):
    def on_day_close(self, day):
        bars = self.ctx.data.history("SZ.000001", "1d", 20)   # 只有过去
        if bars[-1].close > max(b.high for b in bars[:-1]):
            self.ctx.buy("SZ.000001", 1000)                    # 次日事件成交

cfg = SandboxConfig(start="2024-01-01", end="2024-06-28", report=False)
res = SandboxRunner(cfg, [MyStrategy()]).run()
print(res.summary())
```

### 示例 1：live/ 仓位层实盘策略进沙盒

```bash
python examples/run_poslayer.py --start 2024-01-01 --end 2026-10-26 --cap 200000
python examples/run_poslayer.py --speed 86400 --pace    # 1 真实秒 = 1 虚拟日
```

`PosLayerStrategy` 是 `live/strategy.py` 基线（S0/G0/close_next）的**实盘形态**：
每个虚拟交易日收盘用 PIT 数据算特征与指数状态门，报单次日尾盘成交，45 日到期 /
放量减半逐日推进。与向量化基线的诚实差异（sizing 不用次日价、T+1/涨跌停真实
执行等）见 [`adapters/poslayer.py`](src/framesandbox/adapters/poslayer.py) 文档。

### 示例 2：真实分钟回放 · 假信号欺骗演示

```bash
python examples/run_intraday.py --focus-n 60
```

回放真实 1 分钟数仓窗口（2026-09-29 → 2026-10-19，流动性前 60）。实测该窗口
314 次信号触发、142 次假信号（45.2%）：唯一一次盘中突破 20 日高是**假突破**，
313 次跌破里 141 次是假跌破。三个演示策略同场对比（实测结果）：

| 策略 | 应对 | 实测结局 |
|---|---|---|
| `Chaser` | 盘中一突破就追买 | 被假突破骗进场，T+1 当天卖不掉，**-1.38%**（追假 -11,177） |
| `Disciplined` | 等收盘确认仍突破才买 | 一次都没被骗，**0.00%**（代价：该窗口没有入场机会） |
| `StopRunner` | 持仓跌破 20 日低就止损 | 被 16 次假跌破震在低点，**-2.85%**（追假 -5,473） |

报告对比「被假信号欺骗次数 / 追假盈亏 / 收益回撤」—— 应对方式的价值被量化。

### 示例 3：stress 加练

```bash
python examples/run_stress.py --start 2024-01-01 --end 2024-06-28 --prob 0.3
```

在**合成日**主动注入假突破（短暂顶破 N 日高再回落，日线/分钟同步改写），
真实数仓日永不碰。纯历史 vs 注入各跑一遍，看策略应对是否稳健。

### 示例 4：寄生模式 · live_now 项目零改动运行

```bash
python examples/run_live_now_snapshot.py --date 2025-04-07
```

不移植代码，直接 import `live_now/engine.py` 在隔离层内调用它自己的
`load_panel / regime_now / current_signal` —— 数据入口已被劫持到 PIT 网关，
项目代码以为自己在「实时」，其实活在历史里。适合单点快照型实盘脚本。

### 示例 5：evf-trade-agent 桥接 · 一次开发，验证后实盘

```bash
python examples/run_evf_agent.py --start 2024-01-01 --end 2024-06-28
python examples/run_evf_agent.py --start 2026-09-29 --end 2026-10-19 --resolution 1m
# 或在 evf-trade-agent 侧：python verify_sandbox.py --start 2024-01-01 --end 2024-06-28
```

trade-agent 的注册策略 + Executor 执行层**原样**进沙盒：行情走 PIT Shim、
成交走 PaperBroker，策略代码与 config 两边共用同一份，零改动。1m 模式在
虚拟 14:50 用 forming 价决策（与实盘同构），每分钟独立止损。联动架构与
验收门见 [`docs/INTEGRATION_TRADE_AGENT.md`](docs/INTEGRATION_TRADE_AGENT.md)。

## 核心机制

| 模块 | 能力 |
|---|---|
| [`clock.py`](src/framesandbox/clock.py) | 权威虚拟时钟：整数微秒、私有 key 推进、公开只读诱饵、加速倍率 |
| [`gateway.py`](src/framesandbox/gateway.py) | PIT 网关 G1–G6：钳制/违规异常/副本/审计/时钟只读/forming 显式分离 |
| [`data/`](src/framesandbox/data/) | 全量数仓读取（引擎侧）+ 逐日剧本 + 确定性合成日内桥 + 懒物化 |
| [`broker.py`](src/framesandbox/broker.py) | PaperBroker：T+1/分板涨跌停/整手/成本/延迟管线/预约单（复用 framebt Ledger/Cost） |
| [`deception.py`](src/framesandbox/deception.py) | 假信号检测（突破/跌破/金叉 → 收盘判定 → 追单归因）+ 对抗注入（仅合成日） |
| [`isolation.py`](src/framesandbox/isolation.py) | 时间补丁/数据入口劫持/断网，退出全恢复 |
| [`runner.py`](src/framesandbox/runner.py) | 编排：开盘→分钟→收盘→权益快照；节流加速；确定性 |
| [`report.py`](src/framesandbox/report.py) | 自包含 HTML + CSV + JSON：欺骗统计/追假明细/清洁度审计 |

不变式（测试判据）：

| # | 不变式 |
|---|---|
| G1 | 供出记录时间戳 <= 虚拟现在（日线收盘后才含今日；分钟只供已完成） |
| G2 | 显式未来请求 → `LookaheadViolation` + 审计留痕 |
| G3 | 返回副本/冻结对象，策略改不动引擎 |
| G4 | 每次数据访问留痕；引擎自身供出未来大声失败 |
| G5 | 时钟公开 API 只读，篡改 → `ClockWriteViolation` |
| B1 | 订单延迟管线（默认下一事件成交，I2 同构） |
| B2 | T+1 / 涨跌停 / 整手 / 现金检查真实执行 |
| D1 | 同配置逐位复现；短区间 == 长区间前缀 |

## 产出

每次运行在 `runs/<name>/` 生成：

```
report.html      # 概览/权益曲线/假信号统计/追假明细/拒单分布/清洁度
equity.csv       # 逐日权益/现金/持仓数
fills.csv        # 逐笔成交（含成本）
orders.csv       # 全部订单（含预约/拒单原因）
signals.csv      # 每次信号触发与判定（held/fake、MFE/MAE、追单盈亏）
injections.csv   # stress 注入明细（如有）
summary.json     # 机器可读汇总 + 配置 + 审计
```

## 性能参考（本机实测）

| 场景 | 规模 | 耗时 |
|---|---|---|
| poslayer 日线回放 | 2024 全年 242 交易日 × 5576 标的 | ~40s |
| poslayer 日线回放 | 2024-01 → 2026-10（~680 日） | ~2min |
| 日内 1m 回放 | 10 日 × 60 标的 × 240 分钟 | ~160s/策略 |
| evf fusion 日线回放（桥接） | 2024-01 → 2024-02（37 日，P2 优化后） | ~27s |
| evf fusion 1m 回放（桥接） | 2 日 × 200 标的 × 240 分钟 | ~71s |

长历史日线回放用 `resolution="daily"`；分钟级训练用 `1m` + focus 池。
`--speed 86400 --pace` 可把节奏放慢到「1 真实秒 = 1 虚拟日」供人眼观察。

## 已知边界（诚实声明）

1. **合成分钟路径**是日线约束下的确定性随机桥近似，不是历史逐笔事实；
   真实分钟仅覆盖数仓已有交易日（当前 2026-09-29 → 2026-10-19）。
2. **指数无分钟流**：盘中 regime 用昨日收盘（与 live_now 近似口径一致）。
3. **撮合 v1**：整单成交或拒绝，无部分成交/排队/盘口深度；涨跌停按板块幅度
   近似（数仓无 ST 历史名称）；IOC/FOK/TRAILING 未实现。
4. **隔离边界**：Python 防不住私有属性访问与 C 层时间（`np.datetime64('now')`）；
   属「越狱」，由审计与测试发现，不假装能防住（与 framebt I5 同一诚实口径）。
5. **幸存者偏差**继承自面板数据源，沙盒不修复。
6. 沙盒是训练与验证工具，**不构成投资建议**；历史表现不外推。

## 文档

- [`docs/DESIGN.md`](docs/DESIGN.md)：架构、时间模型、G1–G6、确定性、与 framebt 的关系。
- [`docs/STRATEGY_CONTRACT.md`](docs/STRATEGY_CONTRACT.md)：实盘策略进沙盒的约束与规范
  （时间/数据/网络/交易规则、回调时序、确定性义务、接入检查清单）。
- [`docs/INTEGRATION_TRADE_AGENT.md`](docs/INTEGRATION_TRADE_AGENT.md)：与 evf-trade-agent
  联动分析（一次开发 → 沙盒验证 → 直接实盘的桥接架构与风险清单）。
- [`docs/PIPELINE.md`](docs/PIPELINE.md)：全流程 Mermaid 图 + 三个完整案例实录
  （daily 长历史 / 1m 欺骗训练 / 新策略全链路）。
- framebt 本体：[`../framebt/README.md`](../framebt/README.md)、
  [`../framebt/USER_GUIDE.md`](../framebt/USER_GUIDE.md)。

## License

MIT。
