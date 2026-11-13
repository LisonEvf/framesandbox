"""实盘项目适配器：把 framebt ``live/`` 仓位层基线（S0/G0/close_next）逐日跑进沙盒。

这不是 ``live/strategy.simulate`` 的向量化回测 —— 那是「拿着整段历史批量算」；
本适配器是它的**实盘形态**：每个虚拟交易日收盘，用网关 PIT 数据算特征与指数
状态门，向 PaperBroker 报单，次日尾盘成交（close_next），持仓管理（45 日到期 /
放量 2.5× 减半）逐日推进。策略感知到的是加速的历史时间，看不到任何未来。

与向量化基线的**诚实差异**（沙盒更严格，结果允许小幅偏离）：
  1.  sizing 用决策日收盘价（基线用次日成交价定数量 —— 那是隐性前视）；
  2.  持仓从成交日起标记（基线把待成交仓位按决策日收盘标记）；
  3.  默认 100 股整手（基线允许碎股，可 ``lot=False`` 对齐）；
  4.  T+1 / 涨跌停拒单真实执行（基线不检查涨跌停）；
  5.  成本默认 ``cost_preset="poslayer"``（双边 0.13% 全包）与基线一致。

特征公式与 ``live/strategy.precompute`` / ``market_regime_now.features`` 逐条同源
（RSI14 为 SMA 版、ma250 min_periods=120、sane=6 日有限且 rev5>=-0.35 等）。
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from ..strategy_api import SandboxStrategy

MIN_AMT, MIN_PRICE, MIN_HIST = 5e7, 2.0, 250
BUY = SELL = 0.0013          # 与 cost_preset="poslayer" 配套的名义费率（sizing 用）
WINDOW = 260                 # 滚动特征窗口（>=250 供 ma/hist 口径）


def _robust_pivot(df, index, columns, values):
    """pandas 3.x 全 A 面板 unstack 越界的替代实现（与 live/strategy 同源）。"""
    sub = df[[index, columns, values]].dropna(subset=[values])
    dup = sub.duplicated(subset=[index, columns], keep="first")
    if dup.any():
        sub = sub.drop_duplicates(subset=[index, columns], keep="first")
    du = np.unique(sub[index].to_numpy())
    su = np.unique(sub[columns].to_numpy())
    di = {d: i for i, d in enumerate(du)}
    si = {s: j for j, s in enumerate(su)}
    out = np.full((len(du), len(su)), np.nan)
    out[sub[index].map(di).to_numpy(), sub[columns].map(si).to_numpy()] = \
        sub[values].to_numpy(float)
    return pd.DataFrame(out, index=pd.Index(du, name=index),
                        columns=pd.Index(su, name=columns))


class PosLayerStrategy(SandboxStrategy):
    name = "poslayer(S0/G0/close_next)"

    def __init__(self, *, hold: int = 45, vol_mult: float = 2.5, trim: float = 0.5,
                 k_max: int = 5, lot: bool = True, fill_at: str = "close_next",
                 min_amt: float = MIN_AMT, min_price: float = MIN_PRICE,
                 min_hist: int = MIN_HIST, hist_start: str = "2010-05-05",
                 sane_strict: bool = False):
        self.hold = hold
        self.vol_mult = vol_mult
        self.trim = trim
        self.k_max = k_max
        self.lot = lot
        self.fill_at = fill_at            # close_next | open
        self.min_amt, self.min_price, self.min_hist = min_amt, min_price, min_hist
        self.hist_start = hist_start      # 与 live/strategy.precompute(start=...) 同口径
        # 基线 sane 的 finite6 循环实际只检查当日（每轮 &= 同一行，非滚动 ——
        # 对账发现的基线潜在 bug）。默认复现基线口径；sane_strict=True 用真 6 日有限。
        self.sane_strict = sane_strict
        self.decisions: list[dict] = []
        self._cols: list[str] = []
        self._col_ix: dict = {}
        self._C = self._A = self._V = None       # WINDOW × N 滚动窗
        self._hist = None                        # 累计有效历史计数
        self._pos: dict = {}                     # sym → {entry_ord, sh, px, trimmed}
        self._pending: dict = {}                 # order_id → sym（入场预约）

    # ------------------------------------------------------------------ 初始化

    def on_start(self, ctx):
        super().on_start(ctx)
        panel = ctx.data.panel(full=True)        # PIT：仅 start 之前的已完成历史
        if len(panel) == 0:
            raise RuntimeError("沙盒起点之前无历史数据（需要 warmup）")
        from ..clock import iso_to_ord
        hs = iso_to_ord(self.hist_start)
        self._cols = sorted(panel["symbol"].unique().tolist())
        self._col_ix = {s: i for i, s in enumerate(self._cols)}
        n = len(self._cols)
        # hist 与基线同口径：从 hist_start 起累计有效收盘数（向量化一遍）
        sub_hist = panel[panel["date"] >= hs]
        fin = np.isfinite(sub_hist["close"].to_numpy(float))
        hist_ser = pd.Series(fin).groupby(sub_hist["symbol"].to_numpy()).sum()
        self._hist = hist_ser.reindex(self._cols).fillna(0).to_numpy(float)
        # 滚动窗只用尾段（~420 自然日 >= 260 交易日），避免全量 pivot
        last_ord = int(panel["date"].max())
        tail_sub = panel[panel["date"] >= last_ord - 420]
        close = _robust_pivot(tail_sub, "date", "symbol", "close").sort_index()
        close = close.reindex(columns=self._cols)
        tail = close.tail(WINDOW)
        self._C = np.full((WINDOW, n), np.nan)
        self._A = np.full((WINDOW, n), np.nan)
        self._V = np.full((WINDOW, n), np.nan)
        c = tail.to_numpy(float)
        self._C[WINDOW - len(c):] = c
        for col, mat in (("amount", self._A), ("vol", self._V)):
            p = _robust_pivot(tail_sub, "date", "symbol", col).sort_index()
            p = p.reindex(columns=self._cols).tail(WINDOW)
            v = p.to_numpy(float)
            mat[WINDOW - len(v):] = v
        self._feat = {}

    # ------------------------------------------------------------------ 每日

    def on_day_close(self, day):
        ctx = self.ctx
        today = day["ord"]
        self._append_today(today)
        feat = self._features()
        regime = self._regime()
        self._exits(today, feat)
        n_entry = self._entries(today, feat, regime)
        self.decisions.append({
            "ord": today, "date": day["date_str"], "regime_on": bool(regime),
            "n_pos": len(self._pos), "entries": n_entry,
            "rsi14": self._regime_info.get("rsi14"),
        })

    def _append_today(self, today):
        """把今日已完成日线并入滚动窗（动态扩列处理新上市标的）。"""
        df = self.ctx.data.panel(n_days=1)
        df = df[df["date"] == today]
        new = [s for s in df["symbol"].unique() if s not in self._col_ix]
        if new:
            for s in new:
                self._col_ix[s] = len(self._cols)
                self._cols.append(s)
            add = len(new)
            self._C = np.hstack([self._C, np.full((WINDOW, add), np.nan)])
            self._A = np.hstack([self._A, np.full((WINDOW, add), np.nan)])
            self._V = np.hstack([self._V, np.full((WINDOW, add), np.nan)])
            self._hist = np.concatenate([self._hist, np.zeros(add)])
        row_c = np.full(len(self._cols), np.nan)
        row_a = np.full(len(self._cols), np.nan)
        row_v = np.full(len(self._cols), np.nan)
        ix = df["symbol"].map(self._col_ix).to_numpy()
        row_c[ix] = df["close"].to_numpy(float)
        row_a[ix] = df["amount"].to_numpy(float)
        row_v[ix] = df["vol"].to_numpy(float)
        self._C = np.vstack([self._C[1:], row_c])
        self._A = np.vstack([self._A[1:], row_a])
        self._V = np.vstack([self._V[1:], row_v])
        self._hist += np.isfinite(row_c)

    def _features(self) -> dict:
        import warnings
        C, A, V = self._C, self._A, self._V
        with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            rev5 = C[-1] / C[-6] - 1.0
            a20 = A[-20:]
            cnt = np.isfinite(a20).sum(axis=0)
            amt20 = np.where(cnt >= 10, np.nanmean(a20, axis=0), np.nan)
            v20 = V[-20:]
            vcnt = np.isfinite(v20).sum(axis=0)
            vma20 = np.where(vcnt >= 10, np.nanmean(v20, axis=0), np.nan)
            vratio = V[-1] / np.where(vma20 > 0, vma20, np.nan)
            finite6 = (np.isfinite(C[-6:]).all(axis=0) if self.sane_strict
                       else np.isfinite(C[-1]))
            sane = finite6 & (rev5 >= -0.35)
        return dict(rev5=rev5, amt20=amt20, vratio=vratio, sane=sane,
                    hist=self._hist, close=C[-1])

    def _regime(self) -> bool:
        s = self.ctx.data.index_series()
        if len(s) < 30:
            self._regime_info = {}
            return False
        close = s.astype(float)
        delta = close.diff()
        up = delta.clip(lower=0).rolling(14, min_periods=7).mean()
        dn = (-delta.clip(upper=0)).rolling(14, min_periods=7).mean()
        rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        ma250 = close.rolling(250, min_periods=120).mean()
        d_ma250 = close / ma250 - 1
        ret20 = close / close.shift(20) - 1
        last = len(close) - 1
        r, d, t = float(rsi.iloc[last]), float(d_ma250.iloc[last]), float(ret20.iloc[last])
        self._regime_info = {"rsi14": r, "d_ma250": d, "ret20": t}
        return bool(r < 30 and d < 0 and t < -0.05)

    # ------------------------------------------------------------------ 出场 / 入场

    def _marks(self) -> dict:
        """以账本持仓为准构建标记价（保证完备，含浮点残留腿）。"""
        marks = {}
        last_px = self.ctx.broker._last_fill_px
        for sym in self.ctx.broker.positions:
            q = self.ctx.data.quote(sym)
            if q is not None and q.last > 0:
                marks[sym] = q.last
            else:
                p = self._pos.get(sym)
                marks[sym] = p["px"] if p else last_px.get(sym, 0.0)
        return marks

    def _exits(self, today, feat):
        ctx = self.ctx
        cal = ctx.data.calendar
        for sym in list(self._pos):
            p = self._pos[sym]
            days = cal.count_between(p["entry_ord"], today)
            avail = ctx.broker.available_to_sell(sym, today)
            if avail <= 0:
                continue
            c = self._col_ix.get(sym)
            vr = feat["vratio"][c] if c is not None else np.nan
            if (self.vol_mult is not None and not p["trimmed"]
                    and np.isfinite(vr) and vr >= self.vol_mult):
                sh = avail * self.trim
                if self.lot:
                    sh = float(int(sh // 100) * 100)
                if sh >= 100:
                    ctx.broker.sell(sym, sh, activate_ord=today, session="close")
                    p["trimmed"] = True
                    p["sh"] -= sh
                    continue
            if days >= self.hold:
                ctx.broker.sell(sym, avail, activate_ord=today, session="close")

    def _entries(self, today, feat, regime) -> int:
        ctx = self.ctx
        slots = self.k_max - len(self._pos) - len(self._pending)
        if slots <= 0 or not regime:
            return 0
        rev5, amt20, sane, hist, close = (feat["rev5"], feat["amt20"], feat["sane"],
                                         feat["hist"], feat["close"])
        elig = (np.isfinite(rev5) & (close > 0) & (amt20 >= self.min_amt)
                & (close >= self.min_price) & (hist >= self.min_hist)
                & (rev5 < 0) & sane)
        for sym in self._pos:
            j = self._col_ix.get(sym)
            if j is not None:
                elig[j] = False
        idx = np.where(elig)[0]
        if len(idx) == 0:
            return 0
        order = idx[np.argsort(rev5[idx])][:slots]
        eq = ctx.broker.equity(self._marks())
        wv = eq / self.k_max
        cal = ctx.data.calendar
        nxt = cal.next_td(today)
        if nxt is None:
            return 0
        n = 0
        for j in order:
            sym = self._cols[j]
            px = close[j]
            if not np.isfinite(px) or px <= 0 or wv / px < 100:
                continue
            sh = wv / (px * (1 + BUY))
            if self.lot:
                sh = float(int(sh // 100) * 100)
                if sh < 100:
                    continue
            session = "open" if self.fill_at == "open" else "close"
            oid = ctx.broker.buy(sym, sh, activate_ord=nxt, session=session)
            self._pending[oid] = sym
            n += 1
        return n

    # ------------------------------------------------------------------ 成交回报

    def on_fill(self, fill):
        from framebt.core.events import OrderSide
        sym = fill.symbol
        if fill.side is OrderSide.BUY:
            self._pending.pop(fill.order_id, None)
            p = self._pos.get(sym)
            if p is None:
                self._pos[sym] = {"entry_ord": fill.frame_id, "sh": fill.size,
                                  "px": fill.price, "trimmed": False}
            else:
                p["sh"] += fill.size
        else:
            p = self._pos.get(sym)
            if p is not None:
                p["sh"] -= fill.size
                if p["sh"] <= 1e-9:
                    self._pos.pop(sym, None)

    def on_reject(self, rec):
        # 入场预约被拒（涨停/现金不足等）→ 释放名额，避免 slots 永久泄漏
        self._pending.pop(rec.get("id"), None)

    def on_stop(self):
        self.ctx.log(f"poslayer 结束：持仓 {len(self._pos)} 只 · 决策 {len(self.decisions)} 天")
