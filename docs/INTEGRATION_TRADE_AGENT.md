# framesandbox × evf-trade-agent 联动分析

> 目标：**一次策略开发，在 framesandbox 回测验证后，直接进 evf-trade-agent 实盘**。
> 本文是可行性分析与集成方案，所有结论均经双方源码核实（引用见各节）。
> 结论先行：**可行，且两个项目的架构天生适合联动** —— 只需新增一个薄桥接层，
> 策略与执行层代码零改动。

---

## 1 · 结论与依据

**可行。** 三条硬依据：

1. **双方都是「契约 + 注册表」架构，边界恰好互补。**
   trade-agent 的策略只输出规范信号（`SignalResult: gate_on + fused`），
   执行层只认契约（`server/strategies/base.py`、`docs/strategies.md`）；
   券商也是契约（`BrokerAdapter`，`server/brokers/base.py`）。
   沙盒恰好能提供这两个契约的「历史回放实现」：PIT 网关当行情源、
   PaperBroker 当券商。策略代码两边跑的是**同一份文件**。

2. **已有成功先例。** framebt 的 `OriginFusionAgentStrategy`
   （`framebt/src/framebt/strategies/origin_fusion_agent.py`）**直接 import
   evf-trade-agent 的 `server.signals`**，逐字节复用 `compute_factors /
   zscore / cluster_pool / limit_threshold`，在因果帧里驱动并补上「行动」一半。
   它连成交量单位换算（数仓「股」→ 快照「手」= vol/100）都已解决并写明口径。
   沙盒联动是同一模式的第三步：framebt 帧回测 → **沙盒实盘时放** → trade-agent 实盘。

3. **沙盒寄生模式已验证过同类需求。** live_now 项目零改动进沙盒
   （`examples/run_live_now_snapshot.py`）：隔离层补丁 `datetime.now /
   time.time / pd.Timestamp.now`、劫持数据入口、断网。trade-agent 的
   时间依赖（见 §5.2）全部落在补丁覆盖范围内。

---

## 2 · 双方边界对照

| 维度 | evf-trade-agent（实盘） | framesandbox（沙盒） | 联动方式 |
|---|---|---|---|
| 策略接口 | `Strategy.compute(snap) → SignalResult`（声明式：门 + 排序榜） | `SandboxStrategy` 回调（命令式：自己下单） | 桥接驱动器把回调翻译成 compute 调用 |
| 行情入口 | `Gateway`（opentdx HTTP，127.0.0.1:8017）：`snapshot/kline/quote` | `PitGateway`：`panel/history/quote/universe` | **Shim 实现 Gateway 接口，底层走 PIT** |
| 券商入口 | `BrokerAdapter`：`place_order/get_account/get_positions/cancel_order` | `PaperBroker`：`buy/sell/cancel/positions/ledger` | **Adapter 实现 BrokerAdapter，底层走 PaperBroker** |
| 执行/风控 | `Executor` + `portfolio`（T+1 台账/止损/熔断/每日 14:50 调仓） | 策略自理（poslayer 适配器自己实现） | **直接复用 Executor**（接 Adapter 后原样运行） |
| 标的口径 | `SH.600519` / `SZ.000001` / `BJ.xxx` | 完全相同 | 无需转换 |
| 时间 | 真实 `datetime.now()` / `time.time()` | 虚拟时钟（隔离层补丁） | 自动生效，无需改代码 |
| 数据源 | opentdx TDX 实时 + QFQ 日线 | 同源 opentdx 数仓（QFQ 日线 + 分钟） | 同源，口径差异见 §6 |
| 参数 | `config.json`（signals/trading 段） | `SandboxConfig` + `strategy_params` | **同一份 config.json 驱动两边** |

关键观察：trade-agent 的 `portfolio.compute_targets / generate_orders /
apply_stop_loss` 是**无 I/O 纯函数**，`Executor` 的 I/O 只有两处
（`broker` 与 `store`）——把这两处换成沙盒实现，执行层逻辑原样复现。

---

## 3 · 集成架构：桥接层三件套

建议放在沙盒侧 `src/framesandbox/adapters/evf_trade_agent.py`
（trade-agent 零改动；import 方式沿用 framebt 先例：`EVF_TRADE_AGENT`
环境变量或默认兄弟目录定位）。

```text
  evf-trade-agent（零改动）                 桥接层（新增 ~500 行）              framesandbox
  ┌────────────────────────┐        ┌──────────────────────────┐        ┌─────────────────┐
  │ Strategy (fusion/自定义) │◄───────│ ① PitGatewayShim          │◄───────│ PitGateway      │
  │  compute(snap)→SignalResult│  snap/ │   实现 Gateway 接口：      │  PIT   │  panel/history/ │
  │ SignalEngine (signals.py)│  kline │   snapshot/kline/quote    │  数据   │  quote/universe │
  ├────────────────────────┤        ├──────────────────────────┤        ├─────────────────┤
  │ Executor + portfolio    │◄───────│ ② SandboxBrokerAdapter    │◄───────│ PaperBroker     │
  │  T+1/止损/熔断/定时调仓   │  委托/  │   实现 BrokerAdapter：     │  撮合   │  T+1/涨跌停/    │
  │  (live 模式, 乐观记账)    │  账户   │   place_order→buy/sell    │  回报   │  整手/成本/延迟  │
  ├────────────────────────┤        ├──────────────────────────┤        ├─────────────────┤
  │ Store(data_dir)         │◄───────│ ③ EvfReplayDriver         │◄───────│ SandboxRunner   │
  │  → 每次运行独立临时目录    │  持久化 │   (SandboxStrategy)       │  事件   │  虚拟时钟/隔离层  │
  └────────────────────────┘        │   事件驱动 compute+risk+    │        │                 │
                                    │   rebalance（替代 Agent 循环）│        │                 │
                                    └──────────────────────────┘        └─────────────────┘
```

### ① PitGatewayShim —— 行情边界

实现 trade-agent `Gateway` 的业务接口（`server/gateway_client.py`），
**不发任何网络请求**：

| Shim 方法 | PIT 实现 | 口径要点 |
|---|---|---|
| `snapshot()` | 1m：`quotes()`（物化标的）；daily：`panel(n_days=1)` + 昨收 | 行字段 `market/code/name/close/pre_close/vol/amount/open/high/low`；**vol 传「手」= 数仓股数/100**（`compute_factors` 内 ×100 还原，framebt 先例同口径）；按涨幅降序（pool 会再按成交额排，顺序不敏感） |
| `kline(symbol, days)` | `history(symbol, "1d", days)` → `[{datetime, open, high, low, close, vol, amount}]` | 数仓 vol 是「股」，原样传（KlineCache 存股）；QFQ 与实盘 `adjust="QFQ"` 一致；**收盘前不含今日** → `compute_factors` 自动追加快照 forming bar，与实盘 14:50 行为同构 |
| `quote(symbol)` | `quote(symbol)` → 规范 dict | 持仓掉出融合榜时的补价通道（`Executor._prices` 的 `quote_getter`） |
| `health()` | 恒 `{"ok": True}` | 无网络 |

- `name`：数仓无历史名称 → 传 `""`（ST 检测失效，见 §6.6；可选静态 ST 列表，
  framebt 先例的 `st_symbols` 参数同款做法）。
- `KlineCache` / `last_snapshot.json` 的落盘由 `data_dir` 隔离（见 ③），
  缓存键是虚拟日期，`time.time()` 已被补丁 → 逐虚拟日刷新，行为正确。

### ② SandboxBrokerAdapter —— 券商边界

实现 `BrokerAdapter` 契约（`server/brokers/base.py`），让 **Executor 以
live 模式运行在 PaperBroker 上**（成交走真实撮合，而非 executor 自带的
无成本纸面成交）：

| 契约方法 | PaperBroker 实现 |
|---|---|
| `place_order(sym, price, vol, side, credit)` | `price>0` → `buy/sell(sym, vol, limit=price)`；`price=-1` → 市价单。返回订单号 |
| `get_account()` | Ledger：`free_amount=cash`、`total_asset=equity(marks)`、`stock_market_value` |
| `get_positions()` | Ledger Position → 规范字段：`vol_hold=net_size()`、`vol_remain=available_to_sell(today)`、`cost_price=avg_price()`、市值/浮盈按最新报价（`Position` API 已具备，`framebt/ledger.py`） |
| `get_orders()` | `order_log` 透传 |
| `cancel_order(oid)` | `broker.cancel(oid)` |
| `health()` | `True` |

语义注意（与实盘 REST 同构，非新增差异）：

- `place_order` 返回 = **报单被接受**，成交在下一事件（延迟管线）——
  Executor 本就按「乐观记账 + 下次 refresh 以券商为准」设计
  （`_live_apply` 注释），沙盒里 PaperBroker 就是那个「券商」。
- 撮合时拒单（涨停/现金/T+1）经 `on_reject` 浮出；下一次 `refresh()`
  以 Ledger 纠正持仓。`today_bought` 台账可能短暂多计被拒买单 →
  T+1 偏保守（安全方向），文档化即可。

### ③ EvfReplayDriver —— 循环边界

`SandboxStrategy` 子类，替代 `Agent._loop`（20s 真实轮询 → 虚拟事件驱动）：

- `on_start`：读 trade-agent `config.json`（单一参数源）；
  `data_dir` = 本次运行独立临时目录（**隔离 trading_state.json /
  signals.jsonl / kline_cache，绝不污染实盘状态**）；
  `make_strategy(shim, cfg, tmp_dir)` 按 `strategy.name` 构造注册策略；
  `Executor(SandboxBrokerAdapter(...), cfg["trading"], Store(tmp_dir),
  quote_getter=shim.quote)`，`set_live(True, confirm="LIVE")`（沙盒内
  的「实盘」= PaperBroker，无真钱）；订阅池 = PIT `universe(pool_size)`。
- **1m 模式**（与实盘同构）：
  - 每分钟：`executor.risk_check(res)`（止损用 `quote_getter` 取最新价，
    **不重算信号**——聚类 SVD 昂贵，见 §7）；
  - 虚拟 14:50：`snap = strategy.snapshot()` → `compute(snap)` →
    `due_for_rebalance()` 为真 → `rebalance(res)`。快照 close = 14:50
    最新价 = **forming 语义（G6）**，与实盘完全一致：盘中假信号会真实
    地骗到策略，沙盒检测器量化被骗代价。
- **daily 模式**（长历史快速验证）：`on_day_close` 用已完成日线
  compute + rebalance。**近似声明**：决策时点从 14:50 forming 变为
  15:00 收盘确认，无盘中假信号暴露（更干净，但与实盘有 10 分钟信息差）。
- `on_fill/on_reject`：记日志；`on_stop`：输出对账摘要。
- **绕过 Agent 层**：不启动 `Agent`（其 LLM 门 `_refresh_gate` 与事件
  检测/看板推送不属于交易链路）；`left_brain` 是确定性纯函数
  （`server/left_brain.py` 明确「无 LLM、逐位可复现」），可随 config 开关。

### 复用矩阵

| 组件 | 沙盒验证时 | 实盘时 | 改动 |
|---|---|---|---|
| 策略实现（`server/strategies/<name>.py`） | 原样 | 原样 | **0** |
| 信号数学（`signals.py`） | 原样 | 原样 | **0** |
| `portfolio.py`（目标/清单/止损纯函数） | 原样 | 原样 | **0** |
| `Executor`（T+1/风控/定时） | 原样（live 模式 + Adapter） | 原样（live + REST） | **0** |
| `config.json`（signals/trading） | 同一份 | 同一份 | **0** |
| 行情 I/O | Shim → PIT | Gateway → TDX | 边界替换 |
| 券商 I/O | Adapter → PaperBroker | RestBroker → 券商 | 边界替换 |
| 循环驱动 | EvfReplayDriver（事件） | Agent._loop（20s） | 边界替换 |
| LLM 门 / 看板 / 告警 | 不启用 | 可选 | 验证范围外 |

---

## 4 · 为什么「验证后直接实盘」成立

1. **同一份策略文件 + 同一份 config**：沙盒里被验证的 `compute` 与实盘
   执行的是同一对象，不存在「回测版 / 实盘版」两套代码的漂移——这正是
   沙盒 README「策略零改动活在历史里」的设计目标。
2. **执行链路同构**：T+1、整手、现金、止损、熔断、每日定时调仓都由
   **同一个 Executor** 执行；沙盒额外施加涨跌停/延迟/成本（比实盘纸面
   更严格），验证只会更保守，不会更乐观。
3. **前视结构性隔绝**：Shim 底层是 PIT 网关，策略不可能在沙盒里偷看
   未来；`lookahead_violations == 0` 是可断言的验收项，而不是代码审查承诺。
4. **假信号应对被量化**：实盘 14:50 用 forming 价决策 = 沙盒 G6 场景；
   欺骗统计（假信号率/追假盈亏）直接回答「这个策略盘中会被骗多惨」。

---

## 5 · 关键技术核对（已逐项验证）

### 5.1 标的与字段口径
- 标的格式双方一致（`SH.600519`）；`sym_of`（market 0/1/2 → SZ/SH/BJ）
  与沙盒数仓市场码一致（framebt 先例 `_market_of` 同约定）。
- 快照必需字段 `close/pre_close/vol/amount/open/high/low/name/market/code`
  均可由 PIT 数据构造（`signals.compute_factors` 实际消费集）。

### 5.2 时间依赖全部可补丁
| trade-agent 调用点 | 用途 | 沙盒覆盖 |
|---|---|---|
| `signals.is_live_session`（`dt.datetime.now`） | 实时/盘前口径切换 | T1 模块属性补丁 ✓ |
| `signals.KlineCache`（`time.time`） | 缓存时效 | T1 ✓ |
| `executor.is_trading_time / due_for_rebalance`（`from datetime import datetime`） | 时段闸/每日定时 | T1 直接引用扫描 ✓ |
| `executor._prepare`（`datetime.now().date`） | T+1 台账翻日 | ✓ |
| `executor` 节流/熔断冷却（`time.time`） | 兜底闸 | ✓（虚拟秒，见 §6.8） |

### 5.3 成交量单位（最易踩的坑，已有先例答案）
数仓 `vol` = **股**；`compute_factors` 把快照 `vol` 当**手** ×100。
Shim 快照行必须传 `vol = bar.volume / 100`，K 线传原值（股）——
与 `origin_fusion_agent.py` 第 163 行完全一致。否则 `relvol5` 放大
100 倍，R 腿全池误触发。

### 5.4 forming 语义对齐
实盘 14:50：K 线缓存只到昨日（今日未收盘），快照 close 注入今日
forming bar（`compute_factors` 的追加/覆盖逻辑）。沙盒 1m 模式 14:50：
`history` 只供已完成日（G1），Shim 快照 = 当前分钟报价 → **同一信息集**。
daily 模式收盘后：`history` 含今日，快照 close == 最后一根日线 close →
`same_bar` 覆盖分支，亦同构。

### 5.5 T+1 双重台账一致
Executor `today_bought`（当日买入不可卖）与 PaperBroker Ledger
`settle_frame = entry_frame + 1` 同向日翻；两者叠加只会更保守。

### 5.6 确定性
`left_brain` 无 LLM、默认关；聚类 SVD 同机逐位可复现；Shim/Adapter/Driver
不引入随机与真实时钟 → 满足沙盒 D1（同配置逐位复现、前缀不变性）。

---

## 6 · 差异与风险（诚实清单）

| # | 差异/风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **localhost 放行**：隔离层不阻断 127.0.0.1（实盘网关 8017 / 券商 12969 都在本机） | 若忘记注入 Shim，寄生代码会**静默取真实数据** → 前视泄漏且不留痕 | 必须显式注入 Shim/Adapter；驱动器启动时断言 `gw` 非真实 Gateway；加「沙盒内禁连 8017/12969」的专项测试 |
| 2 | **LLM 门**：实盘 `Agent._refresh_gate` 会用 LLM 判定覆盖 `gate_on`（`llm_enabled=true` 时） | 沙盒无法复现 LLM 决策 → 门行为不一致 | 验证与自动交易用 `llm_enabled=false`（策略门自治）；LLM 门定位为「验证范围外的人工 overlay」，实盘启用它等于接受未验证偏差 |
| 3 | **成交语义**：executor 自带 paper 成交 = 即时、无成本、无涨跌停；沙盒 PaperBroker = 延迟、有成本、涨停拒买 | 沙盒结果比 trade-agent 纸面**更严格**（方向安全）；乐观记账与成交有短暂偏差 | 走 live 模式 + Adapter（成交经 PaperBroker）；每次调仓前 refresh 以 Ledger 为准；拒单经 on_reject 留痕 |
| 4 | **daily 模式决策时点**：15:00 收盘确认 ≠ 实盘 14:50 forming | 长历史验证无盘中假信号暴露，结果偏干净 | 明确标注近似；关键窗口用 1m 模式复核（欺骗统计量化差异） |
| 5 | **数据 vintage**：数仓 QFQ 是建仓时点复权；实盘 QFQ 随新除权事件漂移；面板继承幸存者偏差 | 除权除息附近历史价格与实盘所见略有差异；退市股不在池 | 沙盒已知边界（README §已知边界）；结果不外推，验证关注行为而非绝对收益 |
| 6 | **无历史名称**：ST 检测（`limit_threshold` 4.8%）失效 | ST 股按 10% 阈值算连板/涨停，驱动簇判定略有偏差 | 与沙盒涨跌停边界一致；可选静态 ST 列表注入（framebt `st_symbols` 先例） |
| 7 | **risk_check 频率**：实盘每 20s 全量 compute；沙盒若每分钟全量算，SVD 聚类不可承受 | 止损触发时点粒度不同 | 信号按日（14:50）/粗网格算；止损每分钟用 `quote_getter` 新鲜价独立跑（Executor 本就支持） |
| 8 | **冷却时间语义**：`dd_cooldown/rebalance_min_gap` 实盘是真实秒，沙盒是虚拟秒 | 对每日一次自动调仓几乎无影响（冷却远短于一日） | 文档化；手动调仓场景不在自动验证范围 |
| 9 | **状态污染**：Executor/Store/KlineCache 都落盘 | 沙盒运行可能覆盖实盘 `trading_state.json`/`signals.jsonl` | `data_dir` 强制指向每次运行独立临时目录（驱动器内建，不可配置到实盘目录） |
| 10 | **自选 extra_symbols**：实盘 compute 带自选补算；沙盒 1m 下非物化自选只有日线级报价 | 自选股因子略有偏差 | 严格模式把自选并集也订阅；或验证时 `extra_symbols=None`（config 开关，文档化） |

---

## 7 · 性能预估（按 README 实测外推）

| 模式 | 规模 | 预估 | 用途 |
|---|---|---|---|
| daily | 全年 242 日 × 全市场面板 + 200 池 SVD/日 | 分钟级（poslayer 同规模 ~40s，fusion 加每日聚类） | 长历史行为验证、参数扫描 |
| 1m | 200 标的 × 240 分钟 × 680 日 | ~10 小时（README：10 日×60 标的≈160s 外推） | **不建议全历史**；用于 1–3 月短窗口欺骗训练（~15–45 分钟） |
| 1m 降本 | focus = 持仓 ∪ 候选（~30–60 标的） | 线性下降 | 池外标的用昨日 PIT 成交额近似排名（文档化近似） |

---

## 8 · 部署流水线（一次开发 → 验证 → 实盘）

```text
① 开发        在 evf-trade-agent/server/strategies/<name>.py 写策略
              （docs/strategies.md 四步：实现 + register + config 选名）
                    │  同一份代码 + 同一份 config.json
② 沙盒验证     EvfReplayDriver 回放（daily 长历史 + 1m 关键窗口）
              验收门：lookahead_violations=0 · isolation_breaches=0
                      同配置逐位复现 · 短区间==长区间前缀
                      读懂欺骗统计与拒单分布（不是简单清零）
③ 纸面观察     trade-agent paper 模式跑一段（docs/strategies.md §6 既有流程）
              与沙盒结果对账（信号/订单 diff，见 P2）
④ 实盘        UI 手输 LIVE / API confirm=LIVE；策略文件零改动
              建议 llm_enabled=false 保持与验证口径一致
⑤ 回归        策略改动 → 重跑 ②（沙盒是 CI 里的验证闸）
```

**晋升门（建议写入团队规范）**：未通过 ② 验收的策略不得进 ④；
沙盒报告（`runs/<name>/summary.json` 清洁度 + 欺骗统计）随策略版本归档。

---

## 9 · 实施路线与工作量

| 阶段 | 内容 | 交付 | 状态 |
|---|---|---|---|
| **P0** | Shim + Adapter + Driver（daily 模式）；`examples/run_evf_agent.py`；data_dir 隔离与「禁连真实网关」断言 | 全历史行为验证可跑，报告产出 | ✅ 2026-11-14 |
| **P1** | 1m 模式 14:50 forming 驱动 + 每分钟止损；focus 降本；欺骗统计解读 | 与实盘同构的盘中验证 | ✅ 2026-11-14 |
| **P2** | 对账测试（沙盒 vs paper 信号/订单 diff）；性能优化（10.5×）；pytest CI 闸 | 「验证即实盘」的可信证据 | ✅ 2026-11-14 |

trade-agent 侧**零改动**（注册表天生可插拔）；沙盒侧新增适配器与示例，
不动核心不变式（G1–G6/B1–B5/D1 全部照常生效）。

---

## 10 · 验收清单（联动实现完成后逐项打勾）

- [ ] 策略文件与 config 在沙盒/实盘为同一份，无分叉代码；
- [ ] Shim 不发网络请求；沙盒进程连 8017/12969 的尝试被测试捕获；
- [ ] `vol` 单位换算有专项测试（relvol5 不被放大 100 倍）；
- [ ] 14:50 forming 信息集与实盘一致（K 线不含今日 + 快照注入）；
- [ ] T+1：当日买入在沙盒与 executor 台账两侧都不可卖；
- [ ] 涨停拒买/跌停拒卖在沙盒真实发生并留痕（实盘纸面不检查）；
- [ ] `data_dir` 隔离：运行后实盘 `data/` 无任何写入；
- [ ] 清洁度：前视违规 0、越狱 0；同配置两次运行逐位一致；
- [ ] 欺骗统计可解释（假信号率/追假盈亏 vs 应对方式）；
- [ ] P2 对账：沙盒 vs paper 信号/订单 diff 在声明容差内。

---

## 11 · 实现状态（2026-11-14：P0 + P1 + P2 已落地）

### 交付物

| 侧 | 文件 | 内容 |
|---|---|---|
| framesandbox | `src/framesandbox/adapters/evf_trade_agent.py` | 三件套：`PitGatewayShim` / `SandboxBrokerAdapter` / `EvfReplayDriver`（含 `paper_reference` 对账臂、内存 K 线缓存旁路） |
| framesandbox | `examples/run_evf_agent.py` | 示例 5：沙盒侧验证入口 |
| framesandbox | `examples/run_evf_parity.py` | P2 对账入口：双臂同跑，产出 `parity.json` |
| framesandbox | `tests/test_evf_bridge.py` | 9 项：股/手换算、PIT、forming、契约字段、T+1、端到端、确定性、前缀不变性、状态隔离 |
| framesandbox | `tests/test_evf_parity.py` | 4 项：信号逐日全等、首日订单全等、执行分叉可归因、双臂确定性 |
| framesandbox | `src/framesandbox/data/warehouse.py` | 性能修复：`symbol_series` 懒构建位置索引（替代全表 Arrow 扫描） |
| evf-trade-agent | `verify_sandbox.py` | 本项目侧验证入口（清洁度未过退出码 1） |
| evf-trade-agent | `tests/test_sandbox_gate.py` | P2 CI 闸：合成数据快速验收（清洁度/对账/确定性，~2s；framesandbox 缺失时优雅跳过） |
| evf-trade-agent | `docs/sandbox.md` | 晋升流水线 + 验收门 + 已知差异 |
| evf-trade-agent | `.gitignore` | 收纳 `sandbox_runs/`（唯一触碰的既有文件，仅加一行） |

trade-agent 策略/执行层代码**零改动**；`config.json` 两边共用同一份。

### 验证结果

- framesandbox：**54 passed**（41 原有 + 9 桥接 + 4 对账）；
  evf-trade-agent：**216 passed, 1 skipped**（含 2 项沙盒闸，零回归）。
- 真实数仓冒烟（daily，2024-01→02，37 日）：+4.71% / 回撤 5.3% / 33 成交 /
  9 拒单（涨停拒买、现金不足、T+1）/ **前视违规 0 · 越狱 0**。
- 真实分钟冒烟（1m，2026-10-15→16）：14:50 管线每日一次、分钟级风控 250 次、
  假信号 53/153（34.6%）/ **双 0**。
- **真实数仓对账（daily，37 日）**：信号分叉 0 天、首日订单全等、订单分叉
  5 天（执行语义所致）；沙盒臂 +4.71% vs 纸面臂 -5.77% —— 下跌市里沙盒的
  拒单/延迟成交反而保护了账户（方向路径依赖，见下）。
- **1m 对账（2 日）**：信号分叉 0、首日订单全等、双 0。
- **性能：280s → 26.7s（同 37 日窗口，10.5×）**，结果逐位一致。
  两处优化：① 引擎 `symbol_series` 首访全表 Arrow 扫描（15M 行 × 数百标的）
  → 一次性 groupby 位置索引；② 桥接层内存 K 线缓存旁路（逐日数百标的
  JSON 读写）+ 快照去冗余排序。

### 实现中发现并处理的问题

1. **沙盒引擎 bug（已修复）**：开盘事件把 `minute_idx` 置 0，但分钟循环
   从 i=1 累计量/额 → 盘中 `Quote.volume/amount/vwap` 漏掉 09:30 首根
   （收盘被日线总量覆盖，日线口径无恙）。桥接的 forming 累计量测试暴露；
   已在 `runner.py` 开盘事件补记首根量/额，全量测试零回归。
2. **live 潜在问题（已文档化，建议修复）**：`SignalEngine._bar_mem` 启动后
   冻结（`warm` 仅启动时一次），长期不重启则 mom20 分母漂移。沙盒驱动器
   每日 `warm` 用正确滚动口径；见 trade-agent `docs/sandbox.md` §6。
3. **乐观记账 T+1 偏差（实测观察到，安全方向）**：昨日下单今日开盘成交的
   买单，沙盒按「今日买入」禁卖，拒掉 Executor 认为可卖的卖单 ——
   PaperBroker 比 Executor 台账更严格，refresh 以账本纠正。
4. **现金预算级联（实测观察到）**：卖出被拒/未成交 → 买入预算落空 →
   现金不足拒单。实盘券商同样会拒，正是验证要暴露的行为。
5. **权益口径（P2 对账发现）**：`Executor.status()["equity"]` 是乐观内部
   账本（拒单也先计持仓），与 PaperBroker 权威盯市可大幅偏离（实测
   659k vs 1047k）。**对账与报告一律以 runner 权益曲线为权威**；
   实盘 UI 每 tick refresh 会纠正，沙盒 daily 模式每日一次。
6. **权益差异方向不预设（P2 实证）**：成交时点/成本/拒单经每日调仓反馈
   路径依赖 —— 下跌市沙盒可能更优（拒单保护），上涨市也可能更优
   （合成面板实证）。对账只要求信号全等 + 首日订单全等 + 分叉可归因。
7. **分位取整与 crossing（P2 测试发现）**：订单价 round 到分、面板价
   4 位小数 → 限价单可能差一分钱开盘不 crossing、收盘才成交。
   这是真实 A 股行为，测试断言须按标的匹配并容差 1 分。

### 后续可选

- 把 `verify_sandbox.py` 接入定时任务（真实数仓周度验收）；
- 1m 长窗口性能（focus 降本 / 信号粗网格）按需优化；
- live `_bar_mem` 修复后，沙盒每日 warm 旁路可保留（语义相同）。

---

## 附：核实出处

| 结论 | 出处 |
|---|---|
| 策略/券商契约 + 注册表 | `evf-trade-agent/server/strategies/base.py`、`brokers/base.py`、`docs/strategies.md`、`docs/brokers.md` |
| 执行层语义（14:50 定时/T+1 台账/止损/熔断/乐观记账） | `server/executor.py`、`server/portfolio.py` |
| 信号数据消费集与 forming 注入 | `server/signals.py`（`compute_factors`、`KlineCache`、`is_live_session`） |
| 复用先例与 vol 单位口径 | `framebt/src/framebt/strategies/origin_fusion_agent.py`（docstring + L163） |
| 沙盒寄生/隔离/不变式 | `framesandbox/src/framesandbox/isolation.py`、`gateway.py`、`broker.py`、`docs/STRATEGY_CONTRACT.md` |
| Ledger 持仓 API（Adapter 映射可行） | `framebt/src/framebt/ledger.py`（`net_size/avg_price/available_to_sell`） |
| 性能基线 | `framesandbox/README.md` 性能参考表 |
