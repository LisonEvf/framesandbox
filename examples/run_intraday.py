"""示例 2：真实分钟数据回放 —— 盘中假信号欺骗演示（2026-09-29 → 2026-10-19）。

这 10 个交易日有真实 1 分钟数仓（opentdx/db/data/intraday）。实测该窗口流动性
前 60 的池子里：314 次信号触发、142 次假信号（45.2%）—— 唯一一次盘中突破
20 日高是**假突破**（收盘回落），另有 313 次跌破（141 次假跌破）。三个演示
策略各跑一遍：

  Chaser      盘中追突破 → 被那一次假突破骗进场（T+1 当天卖不掉），-11,177
  Disciplined 收盘确认才买 → 一次都没被骗（代价：没有入场机会）
  StopRunner  跌破止损 → 被 16 次假跌破震在低点，收盘收回时亏损已锁定

用法：
    python examples/run_intraday.py                     # 默认真实窗口 + 流动性前 60
    python examples/run_intraday.py --only chaser --speed 600 --pace
    python examples/run_intraday.py --start 2026-09-29 --end 2026-10-19
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framesandbox import SandboxConfig, SandboxRunner            # noqa: E402
from fake_breakout_strategies import Chaser, Disciplined, StopRunner  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-29")
    ap.add_argument("--end", default="2026-10-19")
    ap.add_argument("--focus-n", type=int, default=60)
    ap.add_argument("--cap", type=float, default=1_000_000)
    ap.add_argument("--speed", type=float, default=0)
    ap.add_argument("--pace", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--only", default=None, help="只跑某个策略（chaser/disciplined/stoprunner）")
    args = ap.parse_args()

    print(f"framesandbox · 日内假信号演示 · {args.start} → {args.end} · 1m · "
          f"流动性前 {args.focus_n}")
    classes = [Chaser, Disciplined, StopRunner]
    if args.only:
        classes = [c for c in classes if c.__name__.lower() == args.only.lower()]
    summaries = {}
    for cls in classes:
        cfg = SandboxConfig(
            start=args.start, end=args.end, resolution="1m",
            focus_n=args.focus_n, speed=args.speed, pace=args.pace, seed=args.seed,
            initial_cash=args.cap, out_dir=args.out,
            run_name=f"intraday_{cls.__name__.lower()}_{args.start}_{args.end}",
            progress_every=5,
        )
        strat = cls()
        res = SandboxRunner(cfg, [strat]).run()
        s = res.summary()
        summaries[cls.__name__] = (s, res)
        print(f"\n[{strat.name}] 期末 {s['final_equity']:,.0f} · "
              f"收益 {s['total_return']*100:+.2f}% · 回撤 {s['max_drawdown']*100:.1f}%")
        d = s["deception"]
        print(f"  信号 {d['episodes']} · 假信号 {d['fake']}（{d['fake_rate']*100:.1f}%）· "
              f"被追 {d['chased']} · 追假 {d['chased_fake']} · "
              f"追假当日标记盈亏 {d['chase_pnl_mark_fake']:,.0f}")
        print(f"  报告: {res.run_dir}")

    print("\n" + "=" * 72)
    print("对比：应对方式决定被假信号欺骗的代价")
    for name, (s, _) in summaries.items():
        d = s["deception"]
        print(f"  {name:12s} 收益 {s['total_return']*100:+7.2f}% · "
              f"追假 {d['chased_fake']:3d} 次 · 追假盈亏 {d['chase_pnl_mark_fake']:12,.0f}")
    print("（本演示仅供研究沙盒机制，不构成投资建议）")


if __name__ == "__main__":
    main()
