# framesandbox 设计文档

> 基于 framebt 的**实盘时放沙盒**：实盘策略项目在其中运行，感知到的是加速的
> 历史时间与历史数据；盘中假信号会真实地欺骗策略，而数据前视被结构性隔绝。

## 1 · 要解决的问题

| 实盘痛点 | 传统回测的盲区 | 沙盒的机制 |
|---|---|---|
| 盘中假突破骗你追高，收盘回落 | 日线回测只看到「收盘没突破」，信号根本没出现 | 分钟级回放 + forming 数据：信号**盘中真的出现过**，策略真的会追 |
| 回测无意识前视（用今日收盘决策、今日开盘成交） | 靠代码审查 | PIT 网关：一切数据访问钳制到虚拟现在，违规抛异常并留痕 |
| 策略代码「以为现在是 2026 年」 | 回测/实盘两套代码 | 隔离层劫持 `datetime.now`/`time.time`/数据入口/行情网络，策略零改动活在历史里 |
| 实盘事故无法复现 | 随机性/数据版本漂移 | 确定性回放：稳定种子 + 事件全序，同配置逐位复现 |
| 不知道自己被假信号骗了多少次 | 无统计 | 假信号检测器：触发→收盘判定→追单关联→盈亏归因 |

## 2 · 架构

```text
                 ┌────────────────────────── 引擎侧（知道全部历史）─────────────────────────┐
                 │  SandboxData（面板/指数/真实分钟/竞价，全量只读）                          │
                 │        │                                                                  │
                 │  ReplayTimeline（逐日剧本；分钟路径懒物化：真实 > 合成桥 > stress 注入）   │
                 │        │                                                                  │
   真实时间 ──►  VirtualClock（权威虚拟时间，私有 key 推进，公开只读）                        │
                 │        │                                                                  │
                 │  SandboxRunner：09:30 开盘 → 分钟事件 → 15:00 收盘 → 权益快照              │
                 └────────┼──────────────────────────────────────────────────────────────────┘
                          │ 每事件
        ┌─────────────────┼──────────────────────────────┐
        ▼                 ▼                              ▼
  PitGateway        PaperBroker                   FakeSignalDetector
  （策略唯一        （T+1/涨跌停/整手/成本/        （突破/跌破/金叉触发 →
   数据入口，       延迟管线/预约单；复用            收盘 held/fake 判定 →
   G1–G6）          framebt Ledger/Cost）           追单关联）
        │                 │                              │
        └────────┬────────┘                              │
                 ▼                                       │
          SandboxStrategy（on_day_open/on_quote/on_day_close/on_fill）
                 ▲
        Isolation（datetime/time/pd.Timestamp 补丁；load_panel/load_index/
        fetch_sse_realtime 劫持；socket/urlopen 断网；退出全恢复）
```

### 2.1 时间模型

- 虚拟时间 = 整数微秒（UTC epoch），时区固定 Asia/Shanghai。
- **逻辑日 = UNIX 天数**（与面板 `date` 列、`tools._combined_lab.ord_day` 同约定；
  注意不同于 framebt 引擎内部的 `toordinal()`，沙盒自建 Bar 序列，内部自洽）。
- 分钟逻辑时间戳 = `unix_day * 1_000_000 + minute_of_day`（与 `tools/load_intraday` 同构）。
- 加速：`speed` = 虚拟秒/真实秒；`speed=0` 事件驱动尽快推进；`pace=True` 时 Runner
  按真实时间节流（`time.monotonic` 不被补丁，节流不受隔离影响）。
- 写保护：`clock._advance_to(us, _key=...)` 仅 Runner 持有令牌；公开
  `set_now/advance/reset` 一律 `ClockWriteViolation`（framebt I5 同构）。

### 2.2 数据分层与前视隔绝（G1–G6）

- **引擎侧** `SandboxData` 持有全量历史（回放剧本）；**策略侧**只见 `PitGateway`。
- G1 供出记录时间戳 <= 虚拟现在（日线：收盘事件后才含今日；分钟：只供已完成分钟）。
- G2 显式未来请求（`end`/`as_of`/负窗口）→ `LookaheadViolation` + 审计违规清单。
- G3 返回副本/冻结对象（DataFrame.copy、frozen dataclass），策略改不动引擎。
- G4 每次调用留痕（api/参数/行数/最大时间戳）；`_audit` 内置 G1 断言，引擎自身
  供出未来会大声失败（`SandboxError`）。
- G5 时钟只读诱饵。
- G6 `forming()` 显式提供今日形成中日线 —— **合法但自担假信号风险**，与已完成
  历史在 API 上分离。这正是「盘中被假信号欺骗」的入口：策略可以选择用它。
- warmup：回放起点之前的历史照常可得（均线/250 日历史等需要），只钳制「未来」。

### 2.3 日内路径：真实 > 合成 > 注入

- 真实分钟（`opentdx/db/data/intraday/*.parquet`，240 根/日）优先，`source="real"`。
  **复权口径对齐**：数仓分钟价是不复权原始价，日线是 QFQ；除权除息日两者错位
  会制造虚假突破/跌破。回放以开盘价为锚（09:30 分钟价 = 开盘集合竞价价 = 日线
  open）把整条分钟路径缩放到 QFQ 日线口径（`factor = daily_open / raw_first`）；
  成交量本就是未复权原始值，不缩放。
- 缺失时合成确定性随机桥：精确触及日线 O/H/L/C、全程带内、U 型量谱，
  `source="synthetic"`。**合成路径是日线约束下的近似，不是历史逐笔事实**。
- stress 注入（默认关）：只改合成日 —— 短暂顶破前 N 日高再回落，同步改写当日
  日线 high（日线/分钟一致），`source="altered"`。真实数据永不改写。
- 懒物化：只有订阅/持仓/在途订单标的展开分钟路径；其余标的经历「开盘→收盘」
  两事件（长历史日线回放的性能关键）。旧日路径修剪，注入日持久保留。

### 2.4 PaperBroker

- 复用 framebt `Ledger`（多腿/FIFO/T+1）、`StandardCost`、`Order/FillEvent`。
- 延迟管线 `delay_events=1`（I2 同构）：事件 t 下单，最早事件 t+1 成交。
- 预约单 `activate_ord + session(open/close)`：实现「次日开盘/次日尾盘」执行
  （poslayer 基线 close_next 的实盘形态）。
- A 股规则：T+1 可卖量、分板涨跌停（688/300→20%，BJ→30%，其余 10%；ST 无历史
  名称不单独处理）、涨停拒买/跌停拒卖、买入整手、现金检查、只做多。
- v1 边界：整单成交或拒绝，无部分成交/排队/盘口深度；STOP 触发即市价；
  IOC/FOK/TRAILING/STOP_LIMIT 未实现（下单即拒并留痕）。

### 2.5 假信号检测与归因

- 信号规格：`breakout(n)` / `breakdown(n)` / `ma_cross(fast,slow)`，信号位只用
  **已完成历史**在开盘前缓存（日内不变，检测器自身不偷看未来）。
- 触发记录 episode（时刻/价格/信号位/来源），盘中跟踪 MFE/MAE；收盘判定
  `held`（收盘确认）/ `fake`（回落）。
- 追单关联：触发后窗口内同标的同方向下单 → `chased`；按当日收盘标记追单盈亏。
- 报告输出：假信号率、被追次数、追假盈亏、逐笔明细 —— 策略迭代的量化反馈。

### 2.6 隔离层

- T1 时间：`datetime.datetime/date` 替换 + sys.modules 直接引用扫描；
  `time.time/time_ns/localtime/gmtime/ctime`；`pd.Timestamp.now/today/utcnow`。
- T2 数据入口：`_combined_lab.load_panel` → PIT 视图；`market_regime_now.load_index`
  → PIT 指数；`OpentdxParquetSource.load` / `load_intraday_bars` end 钳制；
  `live_signal.fetch_sse_realtime` → None（禁取真实指数）。
- T3 网络：`socket.connect/connect_ex/create_connection`、`urllib.urlopen` 阻断
  （localhost 放行），`IsolationBreach` 留痕。
- 退出全恢复（含异常路径）。
- 诚实边界：Python 防不住私有属性访问与 C 层时间（`np.datetime64('now')`、
  `pd.Timestamp('now')`）—— 属「越狱」，由审计与测试发现，不假装能防住。

## 3 · 确定性

- 合成路径/注入用 `zlib.crc32` 稳定种子（不用加盐的内置 `hash`）。
- 事件全序（天 → 分钟 → 标的排序），逻辑不读真实时钟（节流除外）。
- 测试判据：同配置两次运行权益/成交逐位一致；短区间 == 长区间前缀（前缀不变性）。

## 4 · 与 framebt 的关系

- 复用：`Bar/Order/FillEvent/OrderSide/OrderType`、`Ledger`、`StandardCost`、
  `MarketFrame`（成本计算）、metrics 口径思想、I1–I5 不变式设计。
- 不复用 Engine 调度：沙盒是**事件流回放 + live 风格 API**，不是帧回测；
  framebt 策略可移植为 `SandboxStrategy`（回调签名相近，数据走网关）。
- 数据同源：面板/指数/分钟数仓与 live/、live_now/ 完全一致，结果可对照。
- **对账发现的基线潜在 bug**：`live/strategy.precompute` 的 `finite6` 循环
  每轮 `&=` 同一行（非滚动），「6 日收盘有限」实际只检查当日。适配器默认
  复现基线口径（`sane_strict=False`），可选严格模式；对账测试
  （`tests/test_adapter_parity.py`）逐标的验证 rev5/amt20/vratio/hist/sane。

## 5 · 已知边界（诚实清单）

1. 合成分钟路径非历史逐笔事实；真实分钟仅覆盖数仓已有的交易日。
2. 指数无分钟流：盘中 regime 用昨日收盘（与 live_now 的近似口径一致）。
3. 整单成交、无盘口深度/排队；涨跌停按幅度近似（无 ST 历史）。
4. 面板幸存者偏差继承自数据源（至今存续个股），沙盒不修复。
5. 隔离防不住私有属性/C 层时间越狱；审计负责发现。
6. 策略结果不外推：沙盒是训练与验证工具，不构成投资建议。
