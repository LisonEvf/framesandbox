"""确定性合成日内路径（无数仓分钟数据时的「假信号训练场」）。

为什么需要合成路径
------------------
真实 1 分钟数仓只覆盖最近若干交易日；要在更长历史上让实盘策略体验「盘中被假
信号欺骗」，必须把日线 OHLC 展开成一条**确定性的、可复现的**日内价格路径：

  - 路径精确触及日线的 open / high / low / close（锚点不动）；
  - 全程落在 [low, high] 带内；
  - 高点/低点的出现时刻由种子决定 → 突破可能先真后假（冲高回落），这正是
    日线回测看不到、实盘却天天发生的「假突破」；
  - 成交量按 U 型日内谱分布（开盘/尾盘放量），总量等于日线成交量。

诚实边界
--------
合成路径是**日线约束下的随机桥近似**，不是真实逐笔重建：它制造的分时形态
（冲高回落、探底回升）在统计上合理，但具体分钟级形态不可当成历史事实。
所有合成 Bar / Quote 都带 ``source="synthetic"`` 标记，报告按来源分列。
对抗注入（``deception.AdversarialInjector``）只允许作用于合成日，绝不改真实数据。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..calendar import N_MINUTES, SESSION_MINUTES


@dataclass(frozen=True)
class MinutePath:
    """一条日内分钟路径（240 根，minute-of-day 与 SESSION_MINUTES 对齐）。"""

    minutes: tuple[int, ...]
    prices: tuple[float, ...]
    vols: tuple[float, ...]
    source: str = "synthetic"        # synthetic | real | altered
    altered_note: str = ""

    def __len__(self) -> int:
        return len(self.prices)

    def cum_vol(self, i: int) -> float:
        return math.fsum(self.vols[: i + 1])


def _u_weights(n: int, rng: random.Random) -> list[float]:
    """U 型日内成交量谱 + 抖动。"""
    w = []
    for i in range(n):
        base = 1.0 + 1.6 * math.exp(-i / 22.0) + 1.3 * math.exp(-(n - 1 - i) / 22.0)
        w.append(base * rng.uniform(0.8, 1.2))
    s = math.fsum(w)
    return [x / s for x in w]


def synth_minute_path(
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    *,
    seed: int,
    n: int = N_MINUTES,
) -> MinutePath:
    """由日线 OHLCV 合成确定性分钟路径。

    算法：先随机排布高/低点时刻，构造「开 → 极值1 → 极值2 → 收」的分段线性
    骨架，再在骨架两侧按剩余带宽加有界噪声（锚点强制精确）。骨架与噪声都被
    钳制在 [low, high]，故路径必然精确触及四个锚点且不越带。
    """
    if not (low > 0 and high >= low and high >= max(open_, close) and low <= min(open_, close)):
        raise ValueError(f"非法日线 OHLC: o={open_} h={high} l={low} c={close}")
    rng = random.Random(seed)
    minutes = SESSION_MINUTES[:n] if n <= N_MINUTES else SESSION_MINUTES

    if high == low:  # 一字板/无波动日
        prices = [round(open_, 4)] * n
        w = _u_weights(n, rng)
        return MinutePath(minutes, tuple(prices), tuple(volume * x for x in w), "synthetic")

    # 极值时刻：避开首尾锚点，互不相同
    t_h = rng.randint(2, n - 3)
    t_l = rng.randint(2, n - 3)
    while t_l == t_h:
        t_l = rng.randint(2, n - 3)
    first_is_high = rng.random() < 0.5
    anchors = [(0, open_), (n - 1, close)]
    anchors.append((t_h, high))
    anchors.append((t_l, low))
    anchors.sort()

    # 分段线性骨架
    skeleton = [0.0] * n
    for (i0, p0), (i1, p1) in zip(anchors, anchors[1:]):
        span = i1 - i0
        for k in range(i0, i1 + 1):
            skeleton[k] = p0 + (p1 - p0) * (k - i0) / span

    # 有界噪声（锚点不动）
    anchor_idx = {i for i, _ in anchors}
    prices = []
    for i in range(n):
        p = skeleton[i]
        if i in anchor_idx:
            prices.append(round(p, 4))
            continue
        band = min(p - low, high - p)
        noise = rng.uniform(-0.35, 0.35) * band
        prices.append(round(min(high, max(low, p + noise)), 4))
    # 浮点兜底：强制锚点精确
    for i, p in anchors:
        prices[i] = round(p, 4)

    w = _u_weights(n, rng)
    vols = tuple(volume * x for x in w)
    return MinutePath(minutes, tuple(prices), vols, "synthetic")


def synth_auction_path(
    prev_close: float, open_: float, *, seed: int, n: int = 11
) -> list[tuple[int, float, float]]:
    """合成集合竞价指示价路径（09:15..09:25）：从昨收 wander 到开盘价。

    返回 [(minute_of_day, indicative_price, matched_vol), ...]。
    仅用于「竞价回放」开启且无真实竞价数据时；source 恒为 synthetic。
    """
    from ..calendar import AUCTION_MINUTES
    rng = random.Random(seed ^ 0xA5A5)
    out = []
    n = min(n, len(AUCTION_MINUTES))
    for k in range(n):
        t = k / max(1, n - 1)
        base = prev_close + (open_ - prev_close) * t
        band = abs(open_ - prev_close) * 0.3 + prev_close * 0.001
        p = base + rng.uniform(-band, band) * (1 - t)
        p = max(prev_close * 0.9, min(prev_close * 1.1, p))
        out.append((AUCTION_MINUTES[k], round(p, 4), 100.0 * (k + 1)))
    out[-1] = (AUCTION_MINUTES[n - 1], round(open_, 4), out[-1][2])
    return out
