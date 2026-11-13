"""示例 1：把 live/ 仓位层实盘策略跑进沙盒（加速历史时间 + PIT 数据）。

用法：
    python examples/run_poslayer.py                          # 2024-01-01 → 2026-10-26，日线
    python examples/run_poslayer.py --start 2024-01-01 --end 2024-12-31 --cap 200000
    python examples/run_poslayer.py --resolution 1m --focus-n 20   # 分钟级回放（慢）
    python examples/run_poslayer.py --speed 86400 --pace     # 1 真实秒 = 1 虚拟日（观察节奏）

策略口径与 live/ 基线同源（S0/G0/close_next），差异见 adapters/poslayer.py 文档。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framesandbox import SandboxConfig, SandboxRunner            # noqa: E402
from framesandbox.adapters.poslayer import PosLayerStrategy      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-10-26")
    ap.add_argument("--cap", type=float, default=200_000)
    ap.add_argument("--resolution", default="daily", choices=["daily", "1m"])
    ap.add_argument("--focus-n", type=int, default=30)
    ap.add_argument("--speed", type=float, default=0)
    ap.add_argument("--pace", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    cfg = SandboxConfig(
        start=args.start, end=args.end, resolution=args.resolution,
        speed=args.speed, pace=args.pace, seed=args.seed,
        initial_cash=args.cap, cost_preset="poslayer",
        focus_n=args.focus_n, out_dir=args.out, run_name=args.name,
        progress_every=50,
    )
    strat = PosLayerStrategy()
    print(f"framesandbox · poslayer 实盘形态 · {args.start} → {args.end} · "
          f"{args.resolution} · 本金 {args.cap:,.0f}")
    res = SandboxRunner(cfg, [strat]).run()
    s = res.summary()
    print("=" * 72)
    print(f"期末权益 {s['final_equity']:,.0f} · 收益 {s['total_return']*100:+.2f}% · "
          f"回撤 {s['max_drawdown']*100:.1f}% · 成交 {s['fills']} 笔")
    on_days = sum(1 for d in strat.decisions if d["regime_on"])
    print(f"regime ON 天数 {on_days}/{res.days} · 前视违规 {s['lookahead_violations']} · "
          f"越狱 {s['isolation_breaches']} · 耗时 {s['wall_seconds']}s")
    print(f"报告: {res.run_dir}")


if __name__ == "__main__":
    main()
