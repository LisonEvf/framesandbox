"""引擎侧全量数据读取器（**策略不可见**）。

沙盒的数据分两层：

  1. **引擎侧**（本模块 ``SandboxData``）：持有全量历史 —— 日线面板、上证指数、
     真实分钟/竞价数仓。它是「回放剧本」，只有 Runner / Replay / Gateway 的内部
     实现能碰它。
  2. **策略侧**（``gateway.PitGateway``）：一切访问被钳制到虚拟当前时刻，
     返回副本，逐次审计。策略拿到的是「此刻为止的世界」。

默认数据路径（可用参数 / 环境变量覆盖）：
  - 日线面板：``framebt/tools/cache/daily_panel_v2.parquet``
    （全 A 日线，date 为 ordinal；与 live/ 实盘项目同源）
  - 上证指数：``framebt/tools/sse_index.json``
  - 分钟/竞价：``opentdx/db/data/intraday|auction/YYYY-MM-DD.parquet``

测试可注入内存小面板（``panel=`` / ``index=``），不依赖任何外部文件。
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from datetime import date
from pathlib import Path

from ..calendar import TradingCalendar

_HERE = Path(__file__).resolve()
_TRADE_ROOT = _HERE.parents[4]          # .../TRADE（warehouse.py → data → framesandbox → src → 项目 → TRADE）
_FRAMEBT_ROOT = _TRADE_ROOT / "framebt"

DEFAULT_PANEL = _FRAMEBT_ROOT / "tools" / "cache" / "daily_panel_v2.parquet"
DEFAULT_INDEX = _FRAMEBT_ROOT / "tools" / "sse_index.json"
DEFAULT_DATA_DIR = _TRADE_ROOT / "opentdx" / "db" / "data"

_PANEL_COLS = ["date", "symbol", "open", "high", "low", "close", "vol", "amount"]
_OPT_COLS = ["float_shares", "turnover"]


def _default_data_dir() -> Path:
    env = os.environ.get("FRAMEBT_DATA_DIR")
    return Path(env).expanduser().resolve() if env else DEFAULT_DATA_DIR


class SandboxData:
    """全量历史数据（引擎侧）。所有方法都不做时间钳制——钳制是网关的职责。"""

    def __init__(
        self,
        panel=None,
        index=None,
        *,
        data_dir=None,
        panel_path=None,
        index_path=None,
        intraday_override=None,
        stock_only: bool = True,
        max_symbol_cache: int = 512,
        max_day_cache: int = 64,
        max_intraday_cache: int = 2,
    ):
        import numpy as np
        import pandas as pd

        self._np = np
        self._pd = pd
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else _default_data_dir()
        self._intraday_override = intraday_override or {}
        self._max_symbol_cache = max_symbol_cache
        self._max_day_cache = max_day_cache
        self._max_intraday_cache = max_intraday_cache

        # ---- 日线面板 ----
        if panel is None:
            pp = Path(panel_path) if panel_path else Path(
                os.environ.get("FRAMESANDBOX_PANEL", DEFAULT_PANEL))
            if not pp.exists():
                raise FileNotFoundError(
                    f"日线面板不存在：{pp}。请先在 framebt 里生成 daily_panel_v2.parquet，"
                    "或用 panel= 注入内存面板，或设 FRAMESANDBOX_PANEL。")
            cols = _PANEL_COLS + [c for c in _OPT_COLS]
            panel = pd.read_parquet(pp, columns=[c for c in cols if c != "unused"])
        panel = panel.copy()
        for c in _OPT_COLS:
            if c not in panel.columns:
                panel[c] = 0.0
        panel = panel[_PANEL_COLS + _OPT_COLS]
        panel["date"] = panel["date"].astype("int64")
        if stock_only:
            # 与 live/ 项目 load_panel 同源的「真股票」判定：float_shares 在多数
            # Bar 上为正（指数/ETF 恒为 0，会被剔除）。否则指数会混进候选池。
            fs = panel["float_shares"].fillna(0.0)
            pos = (fs > 0).astype(np.int8)
            frac = pos.groupby(panel["symbol"].to_numpy()).transform("mean")
            panel = panel[frac.to_numpy() >= 0.5].reset_index(drop=True)
        panel = panel.sort_values(["date", "symbol"]).reset_index(drop=True)
        self._panel = panel
        self._dates = np.array(sorted(panel["date"].unique()), dtype=np.int64)
        # date → 行切片 [lo, hi)（searchsorted 向量化求边界）
        darr = panel["date"].to_numpy()
        left = np.searchsorted(darr, self._dates, side="left")
        right = np.searchsorted(darr, self._dates, side="right")
        self._date_bounds = {int(d): (int(a), int(b)) for d, a, b in zip(self._dates, left, right)}
        self._symbols = sorted(panel["symbol"].unique().tolist())
        self._sym_set = set(self._symbols)

        # ---- 上证指数 ----
        if index is None:
            ip = Path(index_path) if index_path else Path(
                os.environ.get("FRAMESANDBOX_INDEX", DEFAULT_INDEX))
            if not ip.exists():
                raise FileNotFoundError(f"指数文件不存在：{ip}（可用 index= 注入）")
            raw = json.loads(ip.read_text(encoding="utf-8"))
            index = pd.Series({pd.Timestamp(k): float(v) for k, v in raw.items()}).sort_index()
            index = index[~index.index.duplicated(keep="last")]
        self._index = index.sort_index()
        self._index_ords = np.array([(t.date() - date(1970, 1, 1)).days
                                     for t in self._index.index], dtype=np.int64)
        self._index_vals = self._index.to_numpy(dtype=float)

        # ---- 缓存 ----
        self._sym_cache: OrderedDict[str, dict] = OrderedDict()
        self._day_cache: OrderedDict[int, dict] = OrderedDict()
        self._intraday_cache: OrderedDict[int, object] = OrderedDict()
        self._intraday_avail: dict[int, bool] = {}
        self._sym_positions: dict | None = None   # 懒构建：symbol → 行位置索引
        self.calendar = TradingCalendar(self._dates.tolist())

    # ------------------------------------------------------------------ 基本元信息

    @property
    def panel(self):
        """全量面板（引擎侧专用；策略侧请走网关）。"""
        return self._panel

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def dates(self):
        return self._dates

    def index_series(self):
        """全量上证指数（引擎侧）。"""
        return self._index

    def index_close(self, ord_day: int) -> float | None:
        import numpy as np
        i = int(np.searchsorted(self._index_ords, int(ord_day)))
        if i < len(self._index_ords) and self._index_ords[i] == int(ord_day):
            return float(self._index_vals[i])
        return None

    # ------------------------------------------------------------------ 日线

    def daily_bars(self, ord_day: int) -> dict:
        """某交易日全市场日线 Bar（向量化构建；pandas 3.x arrow 列 itertuples 极慢）。"""
        from framebt.core.events import Bar

        hit = self._day_cache.get(int(ord_day))
        if hit is not None:
            self._day_cache.move_to_end(int(ord_day))
            return hit
        bounds = self._date_bounds.get(int(ord_day))
        out: dict[str, Bar] = {}
        if bounds is not None:
            sub = self._panel.iloc[bounds[0]:bounds[1]]
            syms = sub["symbol"].to_numpy()
            d = sub["date"].to_numpy()
            o = sub["open"].to_numpy(dtype=float)
            h = sub["high"].to_numpy(dtype=float)
            l = sub["low"].to_numpy(dtype=float)
            c = sub["close"].to_numpy(dtype=float)
            v = sub["vol"].to_numpy(dtype=float)
            a = sub["amount"].to_numpy(dtype=float)
            fs = sub["float_shares"].to_numpy(dtype=float)
            tn = sub["turnover"].to_numpy(dtype=float)
            for i in range(len(syms)):
                s = str(syms[i])
                out[s] = Bar(timestamp=int(d[i]), symbol=s,
                             open=float(o[i]), high=float(h[i]), low=float(l[i]),
                             close=float(c[i]), volume=float(v[i]), amount=float(a[i]),
                             float_shares=float(fs[i]), turnover=float(tn[i]))
        self._day_cache[int(ord_day)] = out
        while len(self._day_cache) > self._max_day_cache:
            self._day_cache.popitem(last=False)
        return out

    def daily_cross_section(self, ord_day: int):
        """某交易日面板切片（DataFrame 视图，引擎侧）。缺日返回空表。"""
        bounds = self._date_bounds.get(int(ord_day))
        if bounds is None:
            return self._panel.iloc[0:0]
        return self._panel.iloc[bounds[0]:bounds[1]]

    def symbol_series(self, symbol: str) -> dict:
        """单标的全部日线（numpy 数组，引擎侧；网关负责钳制与复制）。"""
        import numpy as np
        hit = self._sym_cache.get(symbol)
        if hit is not None:
            self._sym_cache.move_to_end(symbol)
            return hit
        if self._sym_positions is None:
            # 一次性位置索引：避免每个首访标的都对全量面板做 Arrow 字符串
            # 全表扫描（15M 行 × 数百标的曾是桥接回放的主要开销）。
            # 懒构建：不调用 symbol_series 的回放路径（如 poslayer）零开销。
            self._sym_positions = self._panel.groupby("symbol", sort=False).indices
        pos = self._sym_positions.get(symbol)
        sub = self._panel.iloc[pos] if pos is not None else self._panel.iloc[0:0]
        out = {
            "date": sub["date"].to_numpy(dtype=np.int64),
            "open": sub["open"].to_numpy(dtype=float),
            "high": sub["high"].to_numpy(dtype=float),
            "low": sub["low"].to_numpy(dtype=float),
            "close": sub["close"].to_numpy(dtype=float),
            "vol": sub["vol"].to_numpy(dtype=float),
            "amount": sub["amount"].to_numpy(dtype=float),
            "float_shares": sub["float_shares"].to_numpy(dtype=float),
            "turnover": sub["turnover"].to_numpy(dtype=float),
        }
        self._sym_cache[symbol] = out
        while len(self._sym_cache) > self._max_symbol_cache:
            self._sym_cache.popitem(last=False)
        return out

    # ------------------------------------------------------------------ 分钟 / 竞价

    def _date_str(self, ord_day: int) -> str:
        from ..clock import ord_to_date
        return ord_to_date(int(ord_day)).isoformat()

    def has_real_intraday(self, ord_day: int) -> bool:
        o = int(ord_day)
        if o in self._intraday_avail:
            return self._intraday_avail[o]
        ds = self._date_str(o)
        ok = ds in self._intraday_override or (self.data_dir / "intraday" / f"{ds}.parquet").exists()
        self._intraday_avail[o] = ok
        return ok

    def intraday_frame(self, ord_day: int):
        """某日真实 1 分钟行情：DataFrame[symbol_key, minute, price, avg, vol]。

        无数据返回 None。minute = hour*60+minute（09:30 → 570）。
        """
        import numpy as np
        import pandas as pd

        o = int(ord_day)
        hit = self._intraday_cache.get(o)
        if hit is not None:
            self._intraday_cache.move_to_end(o)
            return hit
        ds = self._date_str(o)
        if ds in self._intraday_override:
            df = self._intraday_override[ds]
        else:
            p = self.data_dir / "intraday" / f"{ds}.parquet"
            if not p.exists():
                return None
            df = pd.read_parquet(p, columns=["symbol_key", "datetime", "price", "avg", "vol"])
        if df is None or len(df) == 0:
            return None
        if "minute" not in df.columns:
            dt = pd.to_datetime(df["datetime"])
            df = df.assign(minute=(dt.dt.hour * 60 + dt.dt.minute).astype(np.int64))
        df = df[["symbol_key", "minute", "price", "avg", "vol"]]
        df = df[df["price"] > 0]
        self._intraday_cache[o] = df
        while len(self._intraday_cache) > self._max_intraday_cache:
            self._intraday_cache.popitem(last=False)
        return df

    def auction_frame(self, ord_day: int):
        """某日真实集合竞价（09:15–09:25）：DataFrame[symbol_key, minute, price, matched]。"""
        import numpy as np
        import pandas as pd

        ds = self._date_str(ord_day)
        p = self.data_dir / "auction" / f"{ds}.parquet"
        if not p.exists():
            return None
        df = pd.read_parquet(p, columns=["symbol_key", "datetime", "price", "matched"])
        if len(df) == 0:
            return None
        dt = pd.to_datetime(df["datetime"])
        df = df.assign(minute=(dt.dt.hour * 60 + dt.dt.minute).astype(np.int64))
        return df[["symbol_key", "minute", "price", "matched"]]

    # ------------------------------------------------------------------ 汇总

    def provenance_summary(self, start_ord: int, end_ord: int) -> dict:
        days = self.calendar.range(start_ord, end_ord)
        real = sum(1 for d in days if self.has_real_intraday(d))
        return {"days": len(days), "real_intraday_days": real,
                "synthetic_intraday_days": len(days) - real}
