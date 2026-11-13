"""沙盒回放报告：自包含 HTML + CSV + JSON（零第三方依赖，SVG 手绘曲线）。

报告回答四个问题：
  1. 这次回放「世界」长什么样 —— 区间 / 分辨率 / 真实 vs 合成 vs 注入 数据占比；
  2. 策略被假信号骗了多少次 —— 信号触发 / 假信号率 / 追单 / 按收盘标记的盈亏；
  3. 策略怎么应对的 —— 逐笔追信号明细、拒单原因分布、权益曲线与回撤；
  4. 沙盒自身是否干净 —— 前视违规 0 / 隔离越狱尝试 / 数据访问审计计数。
"""

from __future__ import annotations

import csv
import html
import json
import os

from .clock import ord_to_date


def _fmt_money(x: float) -> str:
    return f"{x:,.0f}"


def _svg_equity(curve: list, width: int = 960, height: int = 260) -> str:
    if len(curve) < 2:
        return "<p>（权益点不足）</p>"
    eqs = [e for _, e, _, _ in curve]
    lo, hi = min(eqs), max(eqs)
    span = (hi - lo) or 1.0
    n = len(eqs)
    pts = []
    for i, e in enumerate(eqs):
        x = 40 + (width - 60) * i / (n - 1)
        y = 10 + (height - 40) * (1 - (e - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    d0 = ord_to_date(curve[0][0]).isoformat()
    d1 = ord_to_date(curve[-1][0]).isoformat()
    return f"""<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px">
<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa" stroke="#ddd"/>
<polyline points="{poly}" fill="none" stroke="#c0392b" stroke-width="1.6"/>
<text x="40" y="{height-8}" font-size="11" fill="#666">{d0}</text>
<text x="{width-120}" y="{height-8}" font-size="11" fill="#666">{d1}</text>
<text x="8" y="16" font-size="11" fill="#666">{hi:,.0f}</text>
<text x="8" y="{height-30}" font-size="11" fill="#666">{lo:,.0f}</text>
</svg>"""


def _table(headers: list, rows: list, max_rows: int = 200) -> str:
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = []
    for r in rows[:max_rows]:
        body.append("<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in r) + "</tr>")
    more = f"<p>… 共 {len(rows)} 行，仅显示前 {max_rows} 行（完整见 CSV）</p>" if len(rows) > max_rows else ""
    return f"<table><thead><tr>{h}</tr></thead><tbody>{''.join(body)}</tbody></table>{more}"


def write_report(result, out_dir: str = "runs", run_name: str | None = None) -> str:
    """写报告目录，返回其路径。"""
    cfg = result.config
    name = run_name or f"{cfg['start']}_{cfg['end']}_{cfg['resolution']}"
    run_dir = os.path.join(out_dir, name)
    os.makedirs(run_dir, exist_ok=True)

    # ---- CSV ----
    with open(os.path.join(run_dir, "equity.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "cash", "n_pos"])
        for o, e, c, n in result.equity_curve:
            w.writerow([ord_to_date(o).isoformat(), round(e, 2), round(c, 2), n])
    with open(os.path.join(run_dir, "fills.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "side", "symbol", "size", "price", "cost", "day", "timestamp"])
        for fl in result.fills:
            w.writerow([int(fl.order_id), fl.side.value, fl.symbol, fl.size,
                        round(fl.price, 4), round(fl.cost, 2),
                        ord_to_date(fl.frame_id).isoformat(), fl.timestamp])
    with open(os.path.join(run_dir, "orders.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        cols = ["id", "symbol", "side", "type", "size", "budget", "price",
                "placed_us", "activate_ord", "session", "state", "reason", "fill_px", "cost"]
        w.writerow(cols)
        for r in result.order_log:
            w.writerow([r.get(c, "") for c in cols])
    with open(os.path.join(run_dir, "signals.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        cols = ["id", "symbol", "kind", "fire_day", "fire_minute", "fire_price", "level",
                "outcome", "close_price", "mfe", "mae", "chased", "chase_pnl_mark", "source"]
        w.writerow(cols)
        for e in result.episodes:
            row = []
            for c in cols:
                v = e.get(c, "")
                if c == "fire_day":
                    v = ord_to_date(v).isoformat() if v else ""
                row.append(v)
            w.writerow(row)
    if result.injections:
        with open(os.path.join(run_dir, "injections.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["day", "symbol", "level", "peak", "t0", "t1", "note"])
            for inj in result.injections:
                w.writerow([ord_to_date(inj["day"]).isoformat(), inj["symbol"],
                            inj["level"], inj["peak"], inj["t0"], inj["t1"], inj["note"]])

    summary = result.summary()
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "config": cfg, "audit": result.audit,
                   "provenance": result.provenance,
                   "isolation_breaches": result.isolation_breaches},
                  f, ensure_ascii=False, indent=2, default=str)

    # ---- HTML ----
    dec = result.deception or {}
    prov = result.provenance or {}
    audit = result.audit or {}
    rej_reasons: dict = {}
    for r in result.rejections:
        key = r["reason"].split("（")[0]
        rej_reasons[key] = rej_reasons.get(key, 0) + 1
    fake_chased = [e for e in result.episodes if e["chased"] and e["outcome"] == "fake"]
    fake_rows = [[ord_to_date(e["fire_day"]).isoformat(), e["symbol"], e["kind"],
                  f"{e['fire_price']:.2f}", f"{e['level']:.2f}",
                  f"{e['close_price']:.2f}", f"{e['mfe']*100:+.2f}%",
                  f"{e['mae']*100:+.2f}%", f"{e['chase_pnl_mark']:,.0f}", e["source"]]
                 for e in fake_chased]
    breach_rows = [[b.get("target", ""), "阻断" if b.get("blocked") else "劫持"]
                   for b in result.isolation_breaches]
    kind_rows = [[k, v["fired"], v["fake"],
                  f"{(v['fake']/v['fired']*100 if v['fired'] else 0):.1f}%",
                  v["chased"], v["chased_fake"]]
                 for k, v in (dec.get("by_kind") or {}).items()]

    html_doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>framesandbox 回放报告 · {html.escape(name)}</title>
<style>
body{{font-family:'Segoe UI',system-ui,sans-serif;margin:24px auto;max-width:1080px;color:#222;line-height:1.55}}
h1{{font-size:22px}} h2{{font-size:17px;border-left:4px solid #c0392b;padding-left:8px;margin-top:28px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left}} th{{background:#f5f5f5}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}} .card{{border:1px solid #ddd;border-radius:8px;padding:10px 16px;min-width:150px}}
.card .v{{font-size:20px;font-weight:600}} .card .k{{font-size:12px;color:#777}}
.warn{{background:#fff6f0;border:1px solid #f0c9a8;padding:8px 12px;border-radius:6px;font-size:13px}}
.ok{{color:#1e8449}} .bad{{color:#c0392b}}
</style></head><body>
<h1>framesandbox 回放报告 · {html.escape(name)}</h1>
<p>区间 {cfg['start']} → {cfg['end']} · 分辨率 {cfg['resolution']} · 种子 {cfg['seed']} ·
加速 {('尽快' if not cfg['speed'] else str(cfg['speed'])+'×')} ·
stress {'开' if cfg['stress'] else '关'} · 耗时 {result.wall_seconds:.1f}s</p>

<h2>1 · 概览</h2>
<div class="cards">
<div class="card"><div class="v">{_fmt_money(result.final_equity)}</div><div class="k">期末权益（初始 {_fmt_money(result.initial_cash)}）</div></div>
<div class="card"><div class="v">{summary['total_return']*100:+.2f}%</div><div class="k">区间收益</div></div>
<div class="card"><div class="v">{summary['max_drawdown']*100:.1f}%</div><div class="k">最大回撤</div></div>
<div class="card"><div class="v">{summary['fills']}</div><div class="k">成交笔数</div></div>
<div class="card"><div class="v">{dec.get('episodes',0)}</div><div class="k">信号触发</div></div>
<div class="card"><div class="v bad">{dec.get('fake',0)}</div><div class="k">假信号（{dec.get('fake_rate',0)*100:.1f}%）</div></div>
<div class="card"><div class="v bad">{dec.get('chased_fake',0)}</div><div class="k">被假信号欺骗（追单）</div></div>
<div class="card"><div class="v">{_fmt_money(dec.get('chase_pnl_mark_fake',0))}</div><div class="k">追假信号当日标记盈亏</div></div>
</div>
{_svg_equity(result.equity_curve)}

<h2>2 · 数据 provenance</h2>
<p>交易日 {prov.get('days',0)} · 真实分钟 {prov.get('real_intraday_days',0)} 日 ·
合成分钟 {prov.get('synthetic_intraday_days',0)} 日 · stress 注入 {len(result.injections)} 次。
合成日 = 日线约束下的确定性随机桥（非历史逐笔事实）；注入只改合成日，真实数据永不改写。</p>

<h2>3 · 信号与假信号</h2>
{_table(['类型','触发','假信号','假信号率','被追','追假'], kind_rows)}
<h3>被假信号欺骗的交易（追单后收盘回落）</h3>
{_table(['日期','标的','类型','触发价','信号位','收盘','MFE','MAE','当日标记盈亏','来源'], fake_rows)}

<h2>4 · 执行与拒单</h2>
{_table(['拒单原因','次数'], sorted(rej_reasons.items(), key=lambda kv:-kv[1]))}

<h2>5 · 沙盒清洁度</h2>
<p>数据访问 {audit.get('total_calls',0)} 次 ·
前视违规 <span class="{'ok' if audit.get('n_violations',0)==0 else 'bad'}">{audit.get('n_violations',0)}</span> ·
隔离越狱 <span class="{'ok' if not result.isolation_breaches else 'bad'}">{len(result.isolation_breaches)}</span></p>
{_table(['目标','处置'], breach_rows) if breach_rows else '<p class="ok">无越狱尝试。</p>'}
<div class="warn">边界声明：合成分钟路径是日线约束下的近似，不是历史逐笔事实；
沙盒 v1 整单成交、无盘口深度；Python 无法阻止私有属性/C 层时间越狱（审计负责发现）。
本报告不构成投资建议。</div>
</body></html>"""
    with open(os.path.join(run_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(html_doc)
    return run_dir
