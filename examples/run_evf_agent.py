"""示例 5：evf-trade-agent 策略桥接 —— 一次开发，沙盒验证，直接实盘。

把 trade-agent 注册表里的策略（config.json 的 strategy.name，默认 fusion）
连同 Executor 执行层原样跑进沙盒：行情走 PIT Shim，成交走 PaperBroker，
策略代码与 config 和实盘共用同一份。

用法：
    python examples/run_evf_agent.py --start 2024-01-01 --end 2024-06-28
    python examples/run_evf_agent.py --resolution 1m --start 2026-09-29 --end 2026-10-19
    python examples/run_evf_agent.py --config ../evf-trade-agent/config.json --cap 200000

验收：前视违规 0 · 越狱 0 · 读懂欺骗统计与拒单分布（见 docs/INTEGRATION_TRADE_AGENT.md）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framesandbox import SandboxConfig, SandboxRunner            # noqa: E402
from framesandbox.adapters.evf_trade_agent import EvfReplayDriver  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2024-06-28")
    ap.add_argument("--resolution", default="daily", choices=["daily", "1m"])
    ap.add_argument("--config", default=None,
                    help="trade-agent config.json（默认 ../evf-trade-agent/config.json）")
    ap.add_argument("--strategy", default=None, help="覆盖 config 的 strategy.name")
    ap.add_argument("--cap", type=float, default=None, help="覆盖 trading.paper_cash")
    ap.add_argument("--pool-n", type=int, default=None, help="覆盖 market.pool_size")
    ap.add_argument("--risk-every", type=int, default=1, help="1m 止损检查间隔（分钟）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--speed", type=float, default=0)
    ap.add_argument("--pace", action="store_true")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else \
        Path(__file__).resolve().parents[2] / "evf-trade-agent" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if args.strategy:
        cfg.setdefault("strategy", {})["name"] = args.strategy
    # 验证口径：LLM 门不参与（沙盒无法复现 LLM 决策，见集成文档 §6.2）
    cfg.setdefault("agent", {})["llm_enabled"] = False
    cap = args.cap if args.cap is not None else float(cfg["trading"]["paper_cash"])

    scfg = SandboxConfig(
        start=args.start, end=args.end, resolution=args.resolution,
        speed=args.speed, pace=args.pace, seed=args.seed,
        initial_cash=cap, cost_preset="default",
        out_dir=args.out, run_name=args.name, progress_every=50,
    )
    driver = EvfReplayDriver(cfg, resolution=args.resolution,
                             pool_size=args.pool_n, risk_every_minutes=args.risk_every)
    print(f"framesandbox × evf-trade-agent · {cfg.get('strategy', {}).get('name', 'fusion')} "
          f"· {args.start} → {args.end} · {args.resolution} · 本金 {cap:,.0f}")
    res = SandboxRunner(scfg, [driver]).run()
    s = res.summary()
    print("=" * 72)
    print(f"期末权益 {s['final_equity']:,.0f} · 收益 {s['total_return']*100:+.2f}% · "
          f"回撤 {s['max_drawdown']*100:.1f}% · 成交 {s['fills']} 笔")
    print(f"调仓 {driver.stats['rebalances']} 次 · 风控 {driver.stats['risk_checks']} 次 · "
          f"拒单 {driver.stats['rejects']} · 耗时 {s['wall_seconds']}s")
    print(f"前视违规 {s['lookahead_violations']} · 越狱 {s['isolation_breaches']} "
          f"（验收要求双 0）")
    if s["deception"]:
        d = s["deception"]
        print(f"假信号 {d['fake']}/{d['episodes']}（{d['fake_rate']*100:.1f}%）· "
              f"被追 {d['chased']} · 追假 {d['chased_fake']} · "
              f"追假盈亏 {d['chase_pnl_mark_fake']:,.0f}")
    print(f"状态隔离目录: {driver.data_dir}")
    print(f"报告: {res.run_dir}")
    if s["lookahead_violations"] or s["isolation_breaches"]:
        print("!! 清洁度验收未通过 —— 不得晋升实盘")
        sys.exit(1)


if __name__ == "__main__":
    main()
