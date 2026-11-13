"""示例 3：stress 模式 —— 在合成日主动注入假突破，给策略「加练」。

纯历史回放（stress=None）与对抗注入（stress={prob:0.3}）各跑一遍同一策略，
对比假信号遭遇数与收益差异。注入只作用于合成日，真实数仓日永不改写。

用法：
    python examples/run_stress.py --start 2024-01-01 --end 2024-06-28
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framesandbox import SandboxConfig, SandboxRunner            # noqa: E402
from fake_breakout_strategies import Chaser                      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2024-06-28")
    ap.add_argument("--focus-n", type=int, default=10)
    ap.add_argument("--prob", type=float, default=0.3)
    ap.add_argument("--cap", type=float, default=1_000_000)
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()

    for label, stress in (("baseline(纯历史)", None),
                          (f"stress(注入p={args.prob})", {"prob": args.prob, "seed": 9})):
        cfg = SandboxConfig(
            start=args.start, end=args.end, resolution="1m",
            focus_n=args.focus_n, initial_cash=args.cap, seed=0,
            stress=stress, out_dir=args.out,
            run_name=f"stress_{label.split('(')[0]}_{args.start}_{args.end}",
            progress_every=20,
        )
        res = SandboxRunner(cfg, [Chaser()]).run()
        s = res.summary()
        d = s["deception"]
        print(f"\n[{label}] 收益 {s['total_return']*100:+.2f}% · "
              f"信号 {d['episodes']} · 假 {d['fake']} · 追假 {d['chased_fake']} · "
              f"注入 {len(res.injections)} 次 · 报告 {res.run_dir}")


if __name__ == "__main__":
    main()
