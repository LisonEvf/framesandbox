# 全流程：一次策略开发 → 沙盒验证 → 直接实盘

> 本文用 Mermaid 绘制 framesandbox × evf-trade-agent 联动的完整流程，
> 并附 2026-11-14 三个完整案例的实测记录（全部通过）。
> 架构细节见 [`INTEGRATION_TRADE_AGENT.md`](INTEGRATION_TRADE_AGENT.md)，
> 策略约束见 [`STRATEGY_CONTRACT.md`](STRATEGY_CONTRACT.md)，
> trade-agent 侧入口见其 `docs/sandbox.md`。

---

## 1 · 晋升流水线（总览）

```mermaid
flowchart TD
    subgraph DEV["① 开发 · evf-trade-agent"]
        D1["server/strategies/&lt;name&gt;.py<br/>实现 Strategy 契约<br/>compute → gate_on + fused"]
        D2["注册表登记<br/>_load_builtins / register_strategy"]
        D3["config.json 选名<br/>strategy.name<br/>（沙盒与实盘共用同一份）"]
        D1 --> D2 --> D3
    end

    subgraph VERIFY["② 沙盒验证 · framesandbox"]
        V0["verify_sandbox.py（agent 侧）<br/>run_evf_agent.py（沙盒侧）"]
        V1["桥接三件套加载<br/>Shim · Adapter · Driver<br/>策略/执行层原样运行"]
        V2["daily 长历史<br/>收盘决策近似 · 快"]
        V3["1m 关键窗口<br/>14:50 forming 同实盘<br/>分钟止损 + 欺骗统计"]
        V4{"验收门<br/>前视违规=0 · 越狱=0<br/>逐位可复现<br/>读懂拒单与欺骗统计"}
        V0 --> V1
        V1 --> V2 --> V4
        V1 --> V3 --> V4
    end

    subgraph PARITY["③ 双臂对账"]
        P1["run_evf_parity.py<br/>验证臂 PaperBroker ∥ 参考臂 paper<br/>同数据 · 同策略 · 同 config"]
        P2{"信号逐日全等<br/>首日订单全等<br/>后续分叉可归因执行语义"}
        P1 --> P2
    end

    subgraph LIVE["④⑤ 纸面 → 实盘 · evf-trade-agent"]
        L1["paper 模式观察<br/>（既有流程）"]
        L2["UI 手输 LIVE / confirm=LIVE<br/>策略文件零改动"]
        L3["Agent 20s 轮询<br/>同一 Executor · 同一风控"]
        L1 --> L2 --> L3
    end

    CI["CI 闸 · pytest tests -q<br/>test_sandbox_gate.py（合成数据 ~2s）<br/>清洁度 + 对账 + 确定性"]

    D3 --> V0
    V4 -->|通过| P1
    V4 -->|未过| FIX["修改策略"]
    P2 -->|通过| L1
    P2 -->|信号分叉| FIX
    FIX --> D1
    L3 -.->|策略改动 → 回归| V0
    CI -.->|每次提交| DEV
    CI -.->|每次提交| VERIFY
```

**硬规则**：未过 ② 验收门与 ③ 对账的策略不得进 ④；沙盒报告随策略版本归档。

---

## 2 · 组件架构（三个 I/O 边界替换，其余零改动）

```mermaid
flowchart TB
    subgraph TA["evf-trade-agent（策略/执行层零改动）"]
        S["Strategy 注册表<br/>fusion · demo_momentum · …"]
        SIG["signals.py 信号数学<br/>compute_factors / 聚类 / z-score"]
        E["Executor + portfolio<br/>T+1 台账 · 止损 · 熔断 · 14:50 定时"]
        S --> SIG
    end

    subgraph BR["桥接层 · framesandbox/adapters/evf_trade_agent.py"]
        SH["① PitGatewayShim<br/>Gateway 接口 → PIT<br/>vol 股→手 · forming 快照"]
        AD["② SandboxBrokerAdapter<br/>BrokerAdapter 契约 → PaperBroker<br/>place_order / account / positions"]
        DR["③ EvfReplayDriver<br/>虚拟事件驱动 替代 Agent 轮询<br/>daily 收盘 / 1m 14:50 + 分钟止损"]
    end

    subgraph SB["framesandbox 引擎"]
        PIT["PitGateway（G1–G6）<br/>前视隔绝 · 审计留痕"]
        PB["PaperBroker（B1–B5）<br/>T+1 · 涨跌停 · 整手 · 成本 · 延迟"]
        RUN["SandboxRunner<br/>虚拟时钟 · 隔离层 · 确定性"]
        DET["FakeSignalDetector<br/>触发 → 收盘判定 → 追单归因"]
    end

    S <-->|"snap / kline / quote"| SH
    E <-->|"委托 / 账户 / 持仓"| AD
    DR -->|"on_day_open / on_quote / on_day_close"| S
    DR -->|"risk_check / rebalance"| E
    SH <--> PIT
    AD <--> PB
    RUN -->|"事件流"| DR
    RUN --> DET
```

---

## 3 · 单个虚拟交易日时序（1m 模式，与实盘同构）

```mermaid
sequenceDiagram
    participant R as SandboxRunner<br/>虚拟时钟
    participant GW as PitGateway<br/>G1–G6
    participant SH as PitGatewayShim
    participant ST as Strategy<br/>（trade-agent 原物）
    participant EX as Executor<br/>（trade-agent 原物）
    participant PB as PaperBroker
    participant DT as FakeSignalDetector

    R->>GW: 09:15 盘前就位
    R->>ST: on_day_open → warm(pool) 刷新 K 线
    R->>PB: 09:30 开盘事件撮合（预约单）
    loop 09:31 – 14:59 每分钟
        R->>PB: 分钟事件撮合（限价/止损单）
        PB-->>EX: on_fill / on_reject（同步）
        R->>DT: on_quote（信号触发记录）
        R->>EX: risk_check（quote_getter 新鲜价止损）
    end
    R->>SH: 14:50 整分钟截面（尾标的触发，无混分钟）
    SH->>GW: quotes(pool) + history（不含今日）
    SH-->>ST: snap（forming 价 = G6，vol 股→手）
    ST-->>EX: SignalResult(gate_on, fused)
    EX->>EX: due_for_rebalance ✓（每日一次）
    EX->>PB: place_order → buy/sell(limit=最新价)
    Note over PB: 14:51 分钟事件成交（延迟管线 B1）<br/>涨停拒买 / T+1 / 现金检查真实执行
    R->>GW: 15:00 收盘定稿（今日日线可得）
    R->>DT: 收盘判定 held/fake + 追单归因
    R->>EX: 收盘 risk_check
    R->>R: 权益快照（PaperBroker 权威盯市）
```

daily 模式差异：无分钟事件，决策在 `on_day_close`（收盘确认，近似声明），
成交在次日开盘事件（限价 crossing）；长历史快验用，关键窗口用 1m 复核。

---

## 4 · 双臂对账数据流

```mermaid
flowchart LR
    CFG["同一份策略 + config.json"] --> A["验证臂<br/>Executor live 模式"]
    CFG --> B["参考臂 paper_reference<br/>Executor 纸面模式"]
    DATA["同一 PIT 数据<br/>同一虚拟时钟"] --> A
    DATA --> B
    A -->|"place_order"| PB["PaperBroker<br/>延迟 · 成本 · 涨跌停 · T+1"]
    B -->|"_paper_fill"| PA["内置纸面<br/>即时 · 无成本"]
    PB --> DIFF["parity.json 对账"]
    PA --> DIFF
    DIFF --> C1["信号逐日全等（必须）"]
    DIFF --> C2["首日订单全等（必须）"]
    DIFF --> C3["后续订单/权益分叉<br/>归因执行语义 · 方向不预设"]
```

权益口径：验证臂以 **runner 权益曲线**（PaperBroker 盯市）为权威；
`Executor.status()` 是乐观内部账本（拒单先计持仓，refresh 纠正），仅作参考。

---

## 5 · 完整案例实录（2026-11-14，全部通过）

### 案例 1 · fusion · daily 长历史 + 对账 + CI 闸

```powershell
# evf-trade-agent 侧
python verify_sandbox.py --start 2024-01-01 --end 2024-06-28 --name case1_daily
python -m pytest tests/test_sandbox_gate.py -q
# framesandbox 侧
python examples\run_evf_parity.py --start 2024-01-01 --end 2024-06-28 --name case1_parity
```

| 项 | 结果 |
|---|---|
| 区间 / 交易日 | 2024-01-01 → 2024-06-28 / 117 日 |
| 期末权益 / 收益 / 回撤 | 856,653 / **-14.33%** / 19.6% |
| 成交 / 拒单 | 379 / 171（涨停拒买·现金不足·T+1） |
| 清洁度 | 前视违规 **0** · 越狱 **0** · 退出码 0 |
| CI 闸 | 2 passed（2.36s） |
| 对账 | 信号分叉 **0** 天 · 首日订单**全等** · 订单分叉 53 天（执行语义） |
| 对账权益 | 沙盒 856,653 vs 纸面 838,444（方向路径依赖） |
| 耗时 | 验证 75.7s（~0.65s/日） |
| 报告 | `evf-trade-agent/sandbox_runs/case1_daily/` · `framesandbox/runs/case1_parity/` |

**解读**：验证闸通过的是行为正确性（无前视、可复现、执行合规）；
策略本身上半年 -14.33% 是诚实的盈亏结论 —— 这正是实盘前该看到的信息。

### 案例 2 · fusion · 1m 真实分钟回放（盘中同构 + 欺骗统计）

```powershell
python examples\run_evf_agent.py --start 2026-09-29 --end 2026-10-19 --resolution 1m --name case2_1m
python examples\run_evf_parity.py --start 2026-09-29 --end 2026-10-19 --resolution 1m --name case2_parity
```

| 项 | 结果 |
|---|---|
| 区间 / 交易日 | 2026-09-29 → 2026-10-19 / 10 日（真实分钟数仓全覆盖） |
| 盘中链路 | 14:50 forming 决策每日一次 · 分钟级风控 **2162** 次 |
| 欺骗统计 | 信号触发 **759** · 假信号 **332（43.7%）** · 被追 **0** |
| 交易 | 全窗口广度门关闭 → 0 成交（门纪律生效，零追假） |
| 清洁度 | 前视违规 **0** · 越狱 **0** |
| 对账 | 信号分叉 **0** · 首日订单全等（空）· 双 0 |
| 耗时 | 57.8s |
| 报告 | `framesandbox/runs/case2_1m/` · `runs/case2_parity/` |

**解读**：近半信号是假的（43.7%），而 fusion 的广度门整个窗口把策略挡在
场外 —— 「门关·仅观察」的纪律被沙盒量化证实：零追假、零损失。
若换成盘中追单策略，同一窗口会被骗 332 次（对照 README 的 Chaser 演示）。

### 案例 3 · 新开发策略 demo_momentum · 全链路（命题直接证明）

```powershell
# ① 开发：server/strategies/demo_momentum.py + 注册表登记（四步法）
# ② 契约测试自动参数化覆盖新实现
python -m pytest tests/test_strategies.py -q          # 11 passed
# ③ 沙盒验证（--strategy 覆盖，不改 config）
python verify_sandbox.py --strategy demo_momentum --start 2024-01-01 --end 2024-06-28 --name case3_demo
# ④ 双臂对账
python examples\run_evf_parity.py --strategy demo_momentum --start 2024-01-01 --end 2024-06-28 --name case3_parity
```

| 项 | 结果 |
|---|---|
| 策略 | **新开发** mom20 单因子排序 + 广度门（复用 compute_factors） |
| 流水线改动 | **0**（注册表可插拔，桥接器自动加载） |
| 契约测试 | 11 passed（自动覆盖新实现） |
| 期末权益 / 收益 / 回撤 | 776,267 / -22.37% / 22.4% |
| 成交 / 拒单 | 93 / 44 |
| 清洁度 | 前视违规 **0** · 越狱 **0** · 退出码 0 |
| 对账 | 信号分叉 **0** · 首日订单**全等** · 订单分叉 12 天 |
| 对账权益 | 沙盒 776,267 vs 纸面 752,517 |
| 报告 | `sandbox_runs/case3_demo/` · `runs/case3_parity/` |

**解读**：从新策略文件落盘到验证/对账全过，**没有改任何流水线代码** ——
「一次策略开发，沙盒验证后直接实盘」的完整命题成立。该策略动量单因子
在 2024 上半年亏损更多（-22.37%），验证闸如实暴露，避免直接上实盘。

### 回归基线

| 套件 | 结果 |
|---|---|
| framesandbox `pytest` | **54 passed** |
| evf-trade-agent `pytest tests -q` | **217 passed, 1 skipped** |

---

## 6 · 复现命令速查

```powershell
# 沙盒侧（framesandbox 目录）
python examples\run_evf_agent.py  --start 2024-01-01 --end 2024-06-28            # daily 验证
python examples\run_evf_agent.py  --start 2026-09-29 --end 2026-10-19 --resolution 1m  # 1m 欺骗训练
python examples\run_evf_parity.py --start 2024-01-01 --end 2024-06-28            # 双臂对账
python -m pytest                                                                  # 全量 54 项

# trade-agent 侧（evf-trade-agent 目录）
python verify_sandbox.py --start 2024-01-01 --end 2024-06-28                     # 验证入口（退出码=验收）
python verify_sandbox.py --strategy demo_momentum                               # 验证候选策略
python -m pytest tests -q                                                        # 全量 + CI 闸
```
