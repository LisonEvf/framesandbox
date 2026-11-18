"""P2 对账示例：沙盒验证臂 vs trade-agent 纸面参考臂（真实数仓）。

两臂同数据、同策略、同 config 在一次回放里并行，产出对账报告：
  - 信号逐日全等（compute 只依赖行情）——任何分叉都是 bug；
  - 首日订单全等（成交尚未分叉）；
  - 此后订单/权益分叉归因于执行语义（延迟/成本/涨跌停/现金拒单）。

用法：
    python examples/run_evf_parity.py --start 2024-01-01 --end 2024-03-29
    python examples/run_evf_parity.py --start 2026-10-15 --end 2026-10-16 --resolution 1m

退出码：0 = 对账通过；1 = 信号/首日订单分叉（不得晋升实盘）。
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
    ap.add_argument("--end", default="2024-03-29")
    ap.add_argument("--resolution", default="daily", choices=["daily", "1m"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--strategy", default=None, help="覆盖 config 的 strategy.name")
    ap.add_argument("--cap", type=float, default=None)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else \
        Path(__file__).resolve().parents[2] / "evf-trade-agent" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if args.strategy:
        cfg.setdefault("strategy", {})["name"] = args.strategy
    cfg.setdefault("agent", {})["llm_enabled"] = False
    cap = args.cap if args.cap is not None else float(cfg["trading"]["paper_cash"])

    scfg = SandboxConfig(start=args.start, end=args.end, resolution=args.resolution,
                         initial_cash=cap, cost_preset="default",
                         out_dir=args.out, run_name=args.name, progress_every=50)
    live = EvfReplayDriver(cfg, resolution=args.resolution)
    paper = EvfReplayDriver(cfg, resolution=args.resolution, paper_reference=True)
    print(f"对账回放 · {args.start} → {args.end} · {args.resolution} · 本金 {cap:,.0f}")
    res = SandboxRunner(scfg, [live, paper]).run()

    # ---- 对账 ----
    sig_diff, ord_diff = [], []
    for a, b in zip(live.decisions, paper.decisions):
        if (a["gate_on"], a["breadth"], a["fused"]) != (b["gate_on"], b["breadth"], b["fused"]):
            sig_diff.append(a["date"])
        if a["orders"] != b["orders"]:
            ord_diff.append(a["date"])
    key = lambda o: (o["side"], o["symbol"], o["volume"], o["price"])
    day1_match = ([key(o) for o in live.decisions[0]["orders"]]
                  == [key(o) for o in paper.decisions[0]["orders"]]) if live.decisions else False
    # 沙盒臂权威权益 = PaperBroker 逐日盯市（runner 权益快照）；
    # executor 内部是乐观记账（拒单也先计持仓），refresh 才纠正 —— 两者都展示
    live_equity = res.equity_curve[-1][1] if res.equity_curve else cap
    report = {
        "days": res.days,
        "signal_mismatch_days": sig_diff,
        "day1_orders_match": day1_match,
        "order_diff_days": ord_diff,
        "live": {"final_equity": round(live_equity, 2),
                 "executor_internal_equity": live.final_status.get("equity"),
                 "fills": len(res.fills), "rejects": live.stats["rejects"],
                 "rebalances": live.stats["rebalances"]},
        "paper": {"final_equity": paper.final_status.get("equity"),
                  "rebalances": paper.stats["rebalances"]},
        "lookahead_violations": res.audit["n_violations"],
        "isolation_breaches": len(res.isolation_breaches),
    }
    if res.run_dir:
        (Path(res.run_dir) / "parity.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = res.summary()
    print("=" * 72)
    has_orders = any(d["orders"] for d in live.decisions)
    diff_note = "（执行语义差异，可解释）" if has_orders else "（门关闭无订单，无执行分叉）"
    print(f"信号分叉天数 {len(sig_diff)}（必须 0）· 首日订单一致 {day1_match} · "
          f"订单分叉天数 {len(ord_diff)}{diff_note}")
    print(f"沙盒臂权益 {report['live']['final_equity']:,.0f} · "
          f"纸面臂权益 {report['paper']['final_equity']:,.0f} · "
          f"成交 {report['live']['fills']} · 拒单 {report['live']['rejects']}")
    print(f"前视违规 {s['lookahead_violations']} · 越狱 {s['isolation_breaches']}")
    print(f"报告: {res.run_dir}")
    if sig_diff or not day1_match or s["lookahead_violations"] or s["isolation_breaches"]:
        print("!! 对账未通过 —— 信号/首日订单必须全等，清洁度必须双 0")
        return 1
    print("对账通过：信号链路逐位一致，执行差异仅来自沙盒更严格的撮合语义。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
