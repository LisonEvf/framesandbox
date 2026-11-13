"""示例 4：寄生模式 —— live_now 项目代码**零改动**在沙盒历史时刻运行。

与适配器模式（把决策逻辑移植成 SandboxStrategy）不同，寄生模式直接 import
实盘项目自己的模块（live_now/engine.py），在隔离层内调用它自己的函数：
``load_panel`` / ``load_index`` 已被劫持到 PIT 网关，``datetime.now`` 是虚拟
时间，真实行情网络被阻断 —— 项目代码以为自己在「实时」，其实活在历史里。

用法：
    python examples/run_live_now_snapshot.py --date 2024-06-28
    python examples/run_live_now_snapshot.py --date 2025-04-07   # 暴跌日，regime 可能 ON
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framesandbox import SandboxConfig, SandboxRunner, SandboxStrategy   # noqa: E402


class LiveNowSnapshot(SandboxStrategy):
    """在虚拟「现在」调用 live_now 项目自己的信号引擎，打印单点快照。"""

    name = "live_now(寄生)"

    def on_day_close(self, day):
        import engine as S          # live_now/engine.py（隔离层已把其数据入口劫持）
        panel = S.lab.load_panel()  # → PIT 面板（绝无未来）
        P = S.precompute(panel)
        on, info = S.regime_now()   # → PIT 指数
        i = len(P["dates"]) - 1
        cands, info, _ = S.current_signal(P, i, on)
        print(f"\n[live_now @ {day['date_str']}] regime {'ON' if on else 'OFF'} · "
              f"rsi14={info['rsi14']:.1f} d_ma250={info['d_ma250']:.3f} "
              f"ret20={info['ret20']:.3f} · 指数截至 {info['as_of']}")
        if cands:
            for c in cands:
                print(f"  #{c['rank']} {c['symbol']} rev5={c['rev5']:.3f} "
                      f"close={c['close']:.2f}")
        else:
            print("  （空仓 —— 非深度超跌状态日，策略逻辑的正确结果）")
        self.ctx.log(f"live_now 快照完成 regime={'ON' if on else 'OFF'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2024-06-28")
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()
    cfg = SandboxConfig(start=args.date, end=args.date, resolution="daily",
                        report=False, progress_every=0, out_dir=args.out)
    print(f"framesandbox · 寄生模式 · live_now 项目零改动运行于 {args.date}")
    res = SandboxRunner(cfg, [LiveNowSnapshot()]).run()
    print(f"\n清洁度：数据访问 {res.audit['total_calls']} 次 · "
          f"前视违规 {res.audit['n_violations']} · 越狱 {len(res.isolation_breaches)}")


if __name__ == "__main__":
    main()
