# 实盘策略沙盒运行约束与规范（Strategy Contract）

> 本文档是**实盘策略项目进入 framesandbox 运行的契约**：沙盒向策略保证什么、
> 策略必须遵守什么、违反了会发生什么。架构原理见 [`DESIGN.md`](DESIGN.md)，
> 本文只讲「接入方需要知道的规则」。
>
> 适用对象：把 live/ 仓位层、live_now/ 快照脚本或任何实盘策略移植/寄生进沙盒的开发者。
> 所有规则均以 `src/framesandbox/` 源码为准；括号内标注出处模块。

---

## 0 · 一页速览

| 维度 | 沙盒保证 | 策略义务 |
|---|---|---|
| 时间 | `datetime.now()` 等全部返回虚拟时间；时钟只读 | 不写时钟、不依赖真实时间、不越狱取 C 层时间 |
| 数据 | 唯一入口 PIT 网关；供出记录 ≤ 虚拟现在；返回副本 | 只走网关取数；显式请求未来会抛异常留痕 |
| 网络 | 真实行情网络阻断（localhost 放行） | 不试图取「真实现在」的任何信息 |
| 交易 | T+1 / 涨跌停 / 整手 / 现金 / 延迟撮合真实执行 | 只用 PaperBroker 下单；处理拒单回报 |
| 欺骗 | 盘中假信号真实出现；触发/判定/追单全留痕 | 用 `forming()` 自担假信号风险；读报告迭代 |
| 确定性 | 同配置逐位复现；短区间 == 长区间前缀 | 随机带种子、迭代有序、逻辑不读真实时钟 |

违规不是「可能被批评」，而是**结构性地抛异常 + 审计留痕**（`errors.py`）：
`LookaheadViolation` / `ClockWriteViolation` / `IsolationBreach` / `SandboxError`。

---

## 1 · 运行环境与时间口径

### 1.1 环境要求（`pyproject.toml`、`__init__.py`）

- Python **3.12+**；目录布局 `TRADE/framebt`、`TRADE/framesandbox`、`TRADE/opentdx`
  同级（framebt 未安装时自动引导其 `src` 进 `sys.path`）。
- 核心（时钟/网关/经纪/检测器）**零第三方依赖**；读真实数仓需 `pandas + pyarrow`。
- 策略代码与沙盒同进程运行；隔离层在 `on_start` 前安装、`on_stop` 后恢复
  （含异常路径），策略的整个生命周期都活在虚拟世界里（`runner.py`、`isolation.py`）。

### 1.2 时间口径（`clock.py`）

| 约定 | 值 |
|---|---|
| 虚拟时间存储 | 整数微秒（UTC epoch 起），无浮点漂移 |
| 时区 | 固定 Asia/Shanghai（UTC+8，无夏令时）；naive datetime 视为上海时区 |
| 逻辑日 `ord` | **UNIX 天数**（1970-01-01 起算），与面板 `date` 列同约定；注意不同于 framebt 引擎内部的 `toordinal()` |
| 分钟逻辑时间戳 | `ord * 1_000_000 + hour*60 + minute`，跨日严格递增 |
| 加速 `speed` | 虚拟秒/真实秒；`0` = 事件驱动尽快推进；`>0` 且 `pace=True` 时按真实时间节流 |

- 每个虚拟交易日从 **09:15** 时钟就位开始；`time.monotonic` **不被补丁**
  （仅供 Runner 节流），策略逻辑不得用它做决策。
- 交易时段（`calendar.py`）：连续竞价分钟网格 = 09:30–11:29 + 13:00–14:59，
  共 **240** 根；09:30 首根含开盘集合竞价撮合，15:00 收盘集合竞价并入日线收盘事件。
  日历从数据推导（不硬编码节假日），数仓覆盖到哪天就能回放到哪天。

---

## 2 · 两种接入方式（`strategy_api.py`）

| 方式 | 做法 | 适用 |
|---|---|---|
| **适配器**（推荐） | 移植为 `SandboxStrategy` 回调：数据全走 `ctx.data` 网关，下单全走 `ctx.broker` | 逐日决策型策略（如 `adapters/poslayer.py`） |
| **隔离寄生** | 项目代码零改动，直接 import 进沙盒进程；隔离层劫持时间/数据入口/网络 | 单点快照型脚本（如 live_now，见 `examples/run_live_now_snapshot.py`） |

寄生模式的额外义务：被劫持的 `load_panel` 返回的是**引擎侧无复制视图**，
项目代码必须**只读**、不得原地修改（`isolation.py`）；需要副本时自行 `.copy()`。

---

## 3 · 时间约束（G5 / I5 同构）

### 3.1 被补丁的时间 API（T1，`isolation.py`）

沙盒内以下调用全部返回**虚拟时间**（含 `sys.modules` 中
`from datetime import datetime/date` 的直接引用扫描替换）：

```
datetime.datetime.now() / today() / utcnow()
datetime.date.today()
time.time() / time_ns() / localtime() / gmtime() / ctime()
pd.Timestamp.now() / today() / utcnow()
```

### 3.2 硬规则

1. **时钟只读**：`clock.set_now / advance / reset` 是诱饵，任何调用抛
   `ClockWriteViolation`；网关的 `set_now` 同样抛违规（`clock.py`、`gateway.py`）。
2. **时间不可回拨**：私有推进通道 `_advance_to(us, _key=...)` 仅 Runner 持有令牌，
   且拒绝回拨；策略拿到 `clock.key` 也无法伪造（同一性校验）。
3. **不依赖真实时间**：决策逻辑只能用 `ctx.now()` / `ctx.today()` /
   `ctx.data.phase()` 或补丁后的时间 API。`speed`/`pace` 只改变播放节奏，
   不改变任何逻辑结果。
4. **越狱边界（诚实声明）**：Python 防不住私有属性访问与 C 层时间
   （`np.datetime64('now')`、`pd.Timestamp('now')`）。这类行为属「越狱」，
   由审计与测试发现，沙盒不假装能防住（与 framebt I5 同一口径）。

---

## 4 · 数据约束（G1–G6，`gateway.py`）

**PitGateway（`ctx.data`）是策略唯一合法数据入口。** 引擎侧的
`SandboxData` / `ReplayTimeline` / `EngineView` 知道全部历史，但策略不可构造、
不可写、不应访问其私有成员。

### 4.1 六条不变式

| # | 不变式 | 对策略的含义 |
|---|---|---|
| G1 | 供出记录时间戳 ≤ 虚拟现在 | 日线：**今日收盘事件后**才含今日；分钟：只供已完成分钟 |
| G2 | 显式请求未来 → `LookaheadViolation` + 审计违规清单 | `history(end=...)` / `panel(as_of=...)` 晚于现在、`n<=0` 等直接抛异常 |
| G3 | 返回副本 / 冻结对象 | `DataFrame.copy()`、frozen dataclass；策略改不动引擎 |
| G4 | 每次调用留痕（api/参数/行数/最大时间戳） | 可统计可断言；引擎自身供出未来会大声失败（`SandboxError`） |
| G5 | 时钟公开 API 只读 | 见 §3 |
| G6 | `forming()` 显式提供今日形成中日线 | **合法但自担假信号风险**，与已完成历史在 API 上分离 |

**warmup**：回放起点之前的历史照常可得（均线 / 250 日历史等需要），
只钳制「未来」。起点之前无历史时适配器会直接报错（需要 warmup）。

### 4.2 数据 API 参考

| API | 语义与边界 |
|---|---|
| `history(symbol, period="1d", n=60, *, end=None)` | 已完成 Bar 升序列表，最多 n 根。`period ∈ {1d, 1m, 5m}`（未知 period 抛 `ValueError`）；`n<=0` 或 `end` 晚于现在 → 违规。**分钟历史只在回放区间内提供**（区间外无分钟，诚实边界） |
| `panel(n_days=300, symbols=None, *, full=False, as_of=None)` | PIT 日线面板**副本**，只含已完成交易日；列与 live/ 面板一致：`date/symbol/open/high/low/close/vol/amount/float_shares/turnover`。`full=True` 取全部历史；`as_of` 晚于现在 → 违规 |
| `index_series()` | PIT 上证指数收盘序列副本（今日收盘后含今日） |
| `universe(n=200, lookback=20)` | PIT 候选池：最近 lookback 个**已完成**交易日日均成交额前 n（要求至少 `lookback//2` 日有数据）；`n<=0` 或 `lookback<=0` → 违规 |
| `quote(symbol)` / `quotes(symbols=None)` | 截至虚拟现在的行情快照（冻结副本），见 §4.3 |
| `forming(symbol)` | 今日正在形成的日线（G6）；未开盘返回 `None` |
| `limits(symbol)` | `(limit_up, limit_down)`，按昨收与分板幅度计算 |
| `subscribe(symbol)` / `unsubscribe(symbol)` / `subscribed()` | 订阅日内分钟流；Runner 在下一事件前物化路径，**盘中订阅立即同步到当前分钟**（不泄露未来） |
| `now()` / `now_us()` / `today()` / `today_str()` / `phase()` | 只读时间；`phase ∈ {pre_open, auction, morning, lunch, afternoon, closed}` |
| `is_trading_day(ord=None)` / `calendar` | 日历只读元数据：`next_td / prev_td / count_between / range / index_of / nth`（持有天数用 `count_between`，口径 (a, b]） |

### 4.3 Quote 快照字段（frozen dataclass）

```
symbol, ts_us, day_ord, minute_of_day(-1=今日尚未开盘),
last, bid, ask, open, high, low, prev_close,
volume(今日累计), amount, vwap, limit_up, limit_down,
halted, source(daily|real|synthetic|altered), phase
```

- **bid/ask 是模拟的 1 tick（0.01 元）价差**（钳制在涨跌停内），不是真实盘口；
  沙盒 v1 无盘口深度（见 §6.5）。
- 未订阅分钟流的标的只有日线级快照（`source="daily"`）：盘前 `last=昨收`，
  盘中 `last=开盘价`（高/低同），收盘后为完整日线。
- 停牌/未上市标的：回退到最近一个有成交日的收盘（PIT），`halted=True`。

### 4.4 数据口径（`data/`）

1. **复权对齐**：数仓分钟价是不复权原始价，日线是 QFQ。回放以开盘价为锚
   （09:30 分钟价 = 开盘集合竞价价 = 日线 open）把整条分钟路径缩放到 QFQ
   口径（`factor = daily_open / raw_first`）；成交量不缩放。策略拿到的
   日线与分钟**同一口径**，除权除息日不会因口径错位制造虚假突破。
2. **日内路径优先级**：真实数仓分钟（`source="real"`）> 确定性合成桥
   （`source="synthetic"`，精确触及日线 O/H/L/C、全程带内、U 型量谱）>
   stress 注入（`source="altered"`，仅合成日，见 §9）。
   **合成路径是日线约束下的近似，不是历史逐笔事实。**
3. **指数无分钟流**：盘中 regime 只能用昨日收盘（与 live_now 近似口径一致）；
   `live_signal.fetch_sse_realtime` 被劫持为返回 `None`（留痕不阻断）。
4. **懒物化**：只有订阅 / 持仓 / 在途订单标的展开分钟路径；其余标的只经历
   「开盘 → 收盘」两事件（长历史日线回放的性能关键）。
5. 回放区间内的日线以剧本为准（含 stress 注入改写，同步进 `panel()`）；
   区间外取原始面板。

---

## 5 · 网络与隔离约束（T2/T3，`isolation.py`）

1. **断网**：`socket.connect / connect_ex / create_connection`、
   `urllib.request.urlopen` 全部阻断，违规抛 `IsolationBreach` 并留痕；
   **localhost（127.0.0.1 / ::1 / localhost）放行**。沙盒内策略拿不到
   「真实现在」的任何信息。
2. **数据入口劫持**（寄生模式自动生效）：
   - `_combined_lab.load_panel` → PIT 面板视图；
   - `market_regime_now.load_index` → PIT 指数（含 `from ... import load_index`
     的直接引用扫描替换）；
   - `OpentdxParquetSource.load` / `load_intraday_bars` → `end` 钳制到最近已完成日；
   - `live_signal.fetch_sse_realtime` → `None`。
3. **退出全恢复**：所有补丁（含异常路径）在沙盒上下文退出时逐一还原。
4. **越狱边界**：私有属性访问、C 层时间、未补丁的第三方取数通道防不住，
   由审计（`isolation_breaches`）与测试发现。策略不得主动尝试。

---

## 6 · 交易约束（B1–B5，`broker.py`）

**PaperBroker（`ctx.broker`）是策略唯一下单入口**，复用 framebt
`Ledger`（多腿/FIFO/T+1）、`StandardCost`、`Order/FillEvent`。

### 6.1 下单与查询 API

| API | 说明 |
|---|---|
| `buy(symbol, shares=0, *, budget=0, limit=None, activate_ord=None, session=None)` | `limit` 有值 → 限价单，否则市价；`budget` 预算单按成交价反算股数 |
| `sell(symbol, shares=0, *, limit=None, activate_ord=None, session=None)` | 同上（卖出不支持 budget） |
| `submit(order, *, activate_ord=None, session=None)` | 直接报 framebt `Order` |
| `cancel(oid)` | 撤未落地订单，返回 bool |
| `cash` / `positions` / `position(symbol)` | 现金；净持仓非零的持仓 dict；单标的持仓 |
| `available_to_sell(symbol, day_ord)` | **T+1 可卖量**（卖出前必查） |
| `equity(marks)` | 按标记价 dict 计算权益；**须为每个持仓提供标记价** |
| `pending_orders()` | 在途订单列表 |

### 6.2 报单即检规则（不合规直接拒单留痕）

- **只做多**：`SELL_SHORT` 拒绝（融券未实现）。
- **订单类型**：仅 `MARKET / LIMIT / STOP`；`IOC / FOK / TRAILING / STOP_LIMIT`
  下单即拒并留痕。
- `size` 与 `budget` 必须至少一个 > 0。
- **买入整手**：默认 `lot=100`，提交时向下取整；不足一手拒单。
  预算单在成交时按 `budget / (成交价 × 1.002)` 反算并取整手，不足一手拒单。
- 预约时段 `session` 只接受 `"open"` / `"close"`。

### 6.3 撮合时序（B1/B5）

- **延迟管线**：默认 `delay_events=1`（framebt I2 同构）——事件 t 下的
  非预约单，最早在该标的**下一个行情事件**（t+1）成交；`delay_events=0`
  可配置为按当前快照立即成交。
- **预约单**：`activate_ord`（最早成交日）+ `session`（open/close 时段），
  **不受延迟管线约束**，用于「次日开盘 / 次日尾盘」执行。注意时序：
  - 撮合发生在回调**之前**（§7.2），所以在 `on_day_close` 里下的
    `session="close"` 单，今日收盘撮合已结束，**最早次日收盘成交**；
    在 `on_day_open` 里下的 `session="open"` 单最早次日开盘成交；
    在 `on_quote` 里下的 `session="close"` 单可当日收盘成交。
- **成交只使用当前事件价格**（B4）——经纪商同样看不到未来。

### 6.4 A 股规则（B2/B3，真实执行）

| 规则 | 语义 |
|---|---|
| T+1 | 卖出量 > `available_to_sell` → 拒单（当日买入不可卖） |
| 涨停拒买 / 跌停拒卖 | 按事件价判定（收盘事件用日线收盘）；幅度分板：688/689/300/301 → **20%**；BJ/920/83/87/43/82/88 → **30%**；其余 **10%**。涨跌停价 = `round(昨收 × (1±幅度), 2)`。**ST 无历史名称，不单独处理**（诚实边界） |
| 停牌 | `halted` 标的不撮合 |
| 现金检查 | 买入需 `股数 × 成交价 × 1.002 ≤ cash`（预留成本），不足拒单 |
| 限价 crossing | 买：`low ≤ limit` 成交于 `min(open, limit)`；卖对称。未触及 → RESTING 挂单等待 |
| STOP | 买：`high ≥ 触发价`；卖：`low ≤ 触发价`；**触发即按事件价市价成交** |

### 6.5 成本与 v1 边界

成本预设（`cost_preset`）：

| 预设 | 口径 |
|---|---|
| `default` | 佣金 3bp（最低 5 元）+ 印花税 5bp（卖）+ 过户费 0.2bp + 滑点 5bp |
| `poslayer` | live/ 基线口径：双边各 0.13% 全包（佣金+滑点合并） |
| `zero` | 无成本（仅调试用） |

**v1 边界（诚实声明）**：整单成交或拒绝，无部分成交 / 排队 / 盘口深度；
IOC/FOK/TRAILING/STOP_LIMIT 未实现。拒单原因全部写入 `orders.csv` 并经
`on_reject` 回调通知。

---

## 7 · 回调契约与事件时序（`strategy_api.py`、`runner.py`）

### 7.1 回调一览（覆写关心的即可，`self.ctx` 在 `on_start` 注入）

| 回调 | 触发时机 | 参数与可用数据 |
|---|---|---|
| `on_start(ctx)` | 回放开始前（隔离已安装） | 注入 `ctx`；可做 warmup（此时网关已有正确的「今天」） |
| `on_day_open(day)` | 09:30 开盘撮合后 | `day={ord, date_str}`；**指数今日收盘此刻未知**；今日日线不可得 |
| `on_quote(quote)` | 1m 模式每个分钟事件（仅订阅/物化标的） | `Quote` 快照（§4.3） |
| `on_day_close(day)` | 15:00 收盘撮合 + 假信号判定后 | `day={ord, date_str, index_close}`；**今日日线已完成**，`history/panel` 含今日 |
| `on_fill(fill)` | 成交瞬间同步插入 | framebt `FillEvent`（`order_id/side/symbol/size/price/frame_id=ord/timestamp/cost`） |
| `on_reject(rec)` | 报单拒单 / 撮合拒单 | `{id, symbol, side, reason}`；**必须处理**（释放名额/回滚状态，参考 poslayer 适配器） |
| `on_stop()` | 回放结束（隔离恢复前） | 收尾日志 |

`SandboxContext`：`data`（网关）、`broker`、`clock`（只读）、
`params`（来自 `config.strategy_params` 的 dict 副本）、
`now()` / `today()` / `log()` / `subscribe()` / `buy()` / `sell()`。
`ctx.log` 写入运行日志（上限 2 万行，超出裁剪——完整留痕以报告 CSV 为准）。

### 7.2 单日事件时序

```
09:15  时钟就位（盘前）；focus 物化（stress 注入在此定稿，开盘前）
09:30  开盘事件：逐标的撮合（预约单在此成交）→ on_day_open
09:31..14:59  （1m 模式）分钟事件：报价更新 → 撮合 → 检测器 → on_quote
15:00  收盘事件：日线定稿 → 逐标的撮合 → 假信号收盘判定 → on_day_close
       → 权益快照（按当日收盘标记，缺报价用最近成交价兜底）
```

关键时序规则：

1. **撮合先于回调**：同一事件内订单先撮合，策略回调后执行（§6.3 的预约单
   时序推论由此而来）。`on_fill` / `on_reject` 在撮合/拒单瞬间同步分发，
   可能穿插在 `on_quote` 之间。
2. **daily 模式**：非物化标的只有开盘/收盘两个事件；收盘事件携带完整日线
   OHLC（限价单可按当日高低价 crossing）。
3. **1m 模式**：分钟事件 `open=high=low=price`；**收盘事件只有收盘价**
   （`open=high=low=close`）——尾盘限价单只能按收盘价 crossing。
4. **关注集** = 订阅标的 ∪ 持仓 ∪ 在途订单标的；盘中 `subscribe` 立即物化
   并同步到当前分钟。
5. **多策略**：按列表顺序分发每个回调，**共享同一账户与网关**；
   `on_fill` 广播给所有策略。策略间不得互相访问私有状态。

---

## 8 · 确定性规范（D1，`runner.py`、`data/replay.py`）

引擎保证：

- 合成路径 / 注入用 `zlib.crc32` 稳定种子（不用加盐的内置 `hash`）；
- 事件全序（天 → 分钟 → 标的排序），逻辑不读真实时钟（节流除外）；
- 测试判据：**同配置两次运行权益/成交逐位一致；短区间 == 长区间前缀**
  （前缀不变性）。

策略侧义务（破坏任何一条都会破坏复现性）：

1. 随机性必须带种子（`random.Random(seed)` / `np.random.default_rng(seed)`），
   种子建议取自 `ctx.params`；
2. 不依赖真实时钟、进程环境、网络等外部不确定输入；
3. 集合/字典迭代影响决策时先排序（标的集合用 `sorted()`）；
4. 决策只依赖 PIT 可得数据——这同时是前视约束与确定性约束；
5. 自检方法：同配置跑两次比对 `equity.csv` / `fills.csv`；再跑一个短区间
   验证前缀不变性。

---

## 9 · 假信号训练与 stress 规范（`deception.py`）

### 9.1 检测器（默认开启，`detect=True`）

- 信号规格：`breakout(n)` / `breakdown(n)` / `ma_cross(fast, slow)`；
  默认 `DEFAULT_SPECS = (breakout(20), breakdown(20))`，可用 `config.specs` 定制。
- 信号位只用**已完成历史**在开盘前缓存，日内不变；每个
  (日, 标的, 信号类型) 每日至多触发一次；检测器自身不偷看未来。
- 收盘判定：`held`（收盘确认）/ `fake`（盘中触发、收盘回落）。
- **追单归因**：触发后 **240 分钟**窗口内同标的同方向下单
  （突破/金叉 → buy，跌破 → sell）标记为 `chased`，按当日收盘标记盈亏。
- 策略的每次盘中信号触发、被骗次数、追假盈亏全部进 `signals.csv` 与报告。

### 9.2 策略应对规范

- 用 `forming()` / 分钟快照做盘中决策是**合法**的（G6），但假信号风险
  自担且被量化——这正是沙盒要训练的场景；
- 「等收盘确认」（`on_day_close` 里用已完成日线决策）不会被盘中假信号骗，
  代价是入场延迟一日（T+1 执行）；两种应对的优劣由报告量化对比；
- 迭代目标：降低 `chased_fake` 次数与 `chase_pnl_mark_fake`，而不是
  单纯提高收益。

### 9.3 stress 对抗注入（默认关闭，`config.stress`）

`AdversarialInjector(prob=0.15, n=20, spike_pct=(0.005, 0.015), hold=(3, 15), seed=0)`：

- **只改合成日**：短暂顶破前 n 日高再回落，同步改写当日日线 high
  （日线/分钟一致），`source="altered"`；**真实数仓日永不改写**；
- 当日已自然突破不叠加；历史不足 `max(5, n//2)` 日不注入；
- 注入明细进 `injections.csv`；用法：纯历史 vs 注入各跑一遍对比稳健性
  （`examples/run_stress.py`）。

---

## 10 · 违规、审计与验收（`errors.py`、`gateway.py`、`report.py`）

### 10.1 异常类型

| 异常 | 含义 | 典型触发 |
|---|---|---|
| `LookaheadViolation` | 前视访问 | `history(end=未来)`、`panel(as_of=未来)`、`n<=0`、网关 `set_now` |
| `ClockWriteViolation` | 篡改时钟 | `clock.set_now/advance/reset`、伪造推进令牌、回拨 |
| `IsolationBreach` | 绕过隔离 | 真实网络取数（localhost 除外） |
| `SandboxError` | 引擎内部不变式破坏 | 理论上不应出现，属引擎 bug（大声失败） |

所有违规同时写入审计（`gateway.violations` / `isolation_breaches`），
供报告与测试断言。

### 10.2 审计与清洁度

- 网关每次调用留痕（api/参数摘要/行数/最大时间戳）；`audit_full=True`
  记录逐条明细；`audit_summary()` 进 `summary.json`。
- 报告含「清洁度」区块：前视违规数、越狱数、拒单分布。
- **验收标准**：一次干净的运行应满足
  `lookahead_violations == 0` 且 `isolation_breaches == 0`；
  拒单（`rejections`）是正常市场约束的体现，应逐条理解而非简单清零。
- 测试判据（`python -m pytest`，41 项）：前视攻击 / 时钟写保护 / T+1 /
  QFQ 对齐 / 口径对账 / 确定性 / 端到端。

---

## 11 · 接入前检查清单

- [ ] 数据只走 `ctx.data` 网关（寄生模式依赖劫持入口），不直读数仓、不取真实行情；
- [ ] 时间只来自 `ctx.now()` / `ctx.today()` 或补丁后的时间 API；
- [ ] 不写时钟、不访问引擎私有成员（`_v` / `_advance_to` / `ledger` 内部等）；
- [ ] 下单只走 `ctx.buy/sell` 或 `broker.submit`；**实现 `on_reject`**（释放名额/回滚状态）；
- [ ] sizing 不用未来价格：决策日收盘价定数量，次日成交价成交
      （用次日价定数量是隐性前视，poslayer 适配器已修正基线此问题）；
- [ ] 卖出前查 `available_to_sell`（T+1）；买入预留现金与整手；
- [ ] 理解预约单时序：`on_day_close` 下的 `session="close"` 单最早次日收盘成交；
- [ ] warmup 充足（如 ma250 需要 ≥250 日历史；`hist_start` 口径与基线一致）；
- [ ] 随机带种子、迭代有序、逻辑不读真实时钟；同配置两次运行逐位一致；
- [ ] 运行后检查 `summary.json`：前视违规 0、越狱 0；理解拒单分布；
- [ ] 读欺骗统计（假信号率 / 追假盈亏），形成应对迭代；
- [ ] 理解 §12 诚实边界，不外推结果。

---

## 12 · 已知边界（诚实声明，与 README 一致）

1. **合成分钟路径**是日线约束下的确定性随机桥近似，不是历史逐笔事实；
   真实分钟仅覆盖数仓已有交易日（当前 2026-09-29 → 2026-10-19）。
2. **指数无分钟流**：盘中 regime 用昨日收盘（与 live_now 近似口径一致）。
3. **撮合 v1**：整单成交或拒绝，无部分成交/排队/盘口深度；涨跌停按板块幅度
   近似（数仓无 ST 历史名称）；IOC/FOK/TRAILING 未实现。
4. **隔离边界**：Python 防不住私有属性访问与 C 层时间；属「越狱」，
   由审计与测试发现，不假装能防住。
5. **幸存者偏差**继承自面板数据源（至今存续个股），沙盒不修复。
6. 沙盒是训练与验证工具，**不构成投资建议**；历史表现不外推。

---

## 附录 A · SandboxConfig 参数速查（`runner.py`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `start` / `end` | `"2024-01-01"` / `"2026-10-26"` | 回放区间（ISO 日期或 UNIX 天数 ord） |
| `resolution` | `"daily"` | `daily`（开盘/收盘两事件）\| `1m`（240 分钟回放） |
| `speed` / `pace` | `0.0` / `False` | 加速倍率；`speed>0 且 pace=True` 按真实时间节流 |
| `seed` | `0` | 合成路径/注入稳定种子 |
| `initial_cash` | `1_000_000` | 初始资金 |
| `cost_preset` | `"default"` | `default` \| `poslayer` \| `zero`（§6.5） |
| `delay_events` | `1` | 延迟管线事件数；0 = 当前快照立即成交 |
| `lot` | `100` | 买入整手股数；0 = 允许碎股 |
| `enforce_limits` | `True` | 涨跌停拒买/拒卖开关 |
| `focus` / `focus_n` | `None` / `30` | 1m 订阅池；None → 自动取 start 前 20 日流动性前 N（引擎侧合法） |
| `stress` | `None` | `AdversarialInjector` 参数字典；None = 纯历史回放 |
| `specs` / `detect` | `DEFAULT_SPECS` / `True` | 假信号规格 / 检测开关 |
| `audit_full` | `False` | 逐条审计明细 |
| `block_network` / `patch_data` | `True` / `True` | 隔离开关（关闭仅用于调试，结果不再可信） |
| `panel_path` / `index_path` / `data_dir` | `None` | 数仓路径覆盖 |
| `progress_every` | `50` | 进度打印间隔（交易日） |
| `strategy_params` | `{}` | 透传给 `ctx.params` 的策略参数 |
| `report` / `out_dir` / `run_name` | `True` / `"runs"` / `None` | 报告开关与输出位置 |

## 附录 B · 运行产出（`runs/<name>/`）

```
report.html      # 概览/权益曲线/假信号统计/追假明细/拒单分布/清洁度
equity.csv       # 逐日权益/现金/持仓数
fills.csv        # 逐笔成交（含成本）
orders.csv       # 全部订单（含预约/拒单原因）
signals.csv      # 每次信号触发与判定（held/fake、MFE/MAE、追单盈亏）
injections.csv   # stress 注入明细（如有）
summary.json     # 机器可读汇总 + 配置 + 审计
```

`result.summary()` 关键字段：`days / final_equity / total_return /
max_drawdown / fills / rejections / deception / audit_calls /
lookahead_violations / isolation_breaches / wall_seconds`。

## 附录 C · 最小接入示例

```python
from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy

class MyStrategy(SandboxStrategy):
    name = "my_live_strategy"

    def on_day_close(self, day):
        bars = self.ctx.data.history("SZ.000001", "1d", 20)   # 只有过去（含今日）
        if bars[-1].close > max(b.high for b in bars[:-1]):
            # 次日尾盘成交（close_next 实盘形态）； sizing 用决策日收盘价
            nxt = self.ctx.data.calendar.next_td(day["ord"])
            sh = int(self.ctx.broker.cash * 0.2 / bars[-1].close / 100) * 100
            if nxt is not None and sh >= 100:
                self.ctx.buy("SZ.000001", sh, activate_ord=nxt, session="close")

    def on_reject(self, rec):
        self.ctx.log(f"拒单 {rec['id']} {rec['symbol']}: {rec['reason']}")

cfg = SandboxConfig(start="2024-01-01", end="2024-06-28", report=False)
res = SandboxRunner(cfg, [MyStrategy()]).run()
print(res.summary())   # 验收：lookahead_violations == 0, isolation_breaches == 0
```

完整范例：`examples/run_poslayer.py`（适配器）、
`examples/run_live_now_snapshot.py`（寄生）、
`examples/run_intraday.py`（分钟欺骗演示）、`examples/run_stress.py`（加练）。
