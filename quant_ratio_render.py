#!/usr/bin/env python3
"""quant-ratio 投资看板 HTML 渲染 v5 — 三模块（量化集中度已下线）

页面: dashboard_investment.html + history/quant_YYYY-MM-DD.html
无硬编码 fallback — 数据缺失显示 "—"，不伪造数值。
index.html 是监控页（勿动）。纯 CSS，无 JS。

v5: ① 国家队资金动向 / ② 今日财经要闻 / ③ 投资信号
    模块① 主指标改为 ETF 份额净申购（旧版用成交额，同日差 350 倍）
"""

import json
import os
import sys
from datetime import date

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
.container { max-width: 1080px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }
.header { background: #1a1a2e; color: white; padding: 20px 24px; display: flex; justify-content: space-between; align-items: center; }
.header h1 { margin: 0 0 6px 0; font-size: 20px; }
.header .meta { font-size: 13px; opacity: 0.75; }
.sig-badge { display:inline-block; padding: 4px 12px; border-radius: 14px; font-size: 13px; font-weight:600; }
.sig-on { background:#e8f5e9; color:#1b5e20; } .sig-off { background:#eceff1; color:#607d8b; }
.section { padding: 20px 24px; border-bottom: 1px solid #eee; }
.section h2 { font-size: 16px; margin: 0 0 4px 0; color: #333; }
.section .sub { font-size: 12px; color:#999; margin-bottom: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid #f0f0f0; }
th { background: #fafafa; color: #666; font-weight: 600; white-space: nowrap; }
tr:hover td { background: #f9f9ff; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.b-on { background:#e8f5e9; color:#2e7d32; } .b-off { background:#fafafa; color:#9e9e9e; }
.up { color:#c62828; font-weight:600; } .down { color:#2e7d32; font-weight:600; } .flat { color:#757575; }
.news-item { display:block; text-decoration:none; color:#1a73e8; font-size:13.5px; line-height:1.6; padding:3px 0; }
.news-item:hover { text-decoration:underline; }
.theme-point { font-size:13.5px; color:#37474f; margin:6px 0; line-height:1.55; }
.theme-point .tname { display:inline-block; background:#e3f2fd; color:#1565c0; border-radius:4px; padding:1px 8px; font-size:12px; margin-right:8px; font-weight:600; }
.methodology { font-size: 12px; color: #888; line-height: 1.75; }
.methodology code { background:#f5f5f5; padding:1px 4px; border-radius:3px; }
.footer { padding: 16px 24px; font-size: 12px; color: #999; text-align: center; }
.dash { color: #bbb; }
.big-num { font-size:26px; font-weight:700; color:#263238; font-variant-numeric:tabular-nums; }
.flow-grid { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:14px; }
.flow-box { border:1px solid #eee; border-radius:8px; padding:10px 16px; min-width:200px; flex:1; }
.table-scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
.arc-list { display:flex; flex-wrap:wrap; gap:8px; }
.arc { font-size:12.5px; color:#1a73e8; text-decoration:none; border:1px solid #e3f2fd; border-radius:6px; padding:4px 10px; white-space:nowrap; }
.arc:hover { background:#f3f8ff; }
.warn-note { background:#fff8e1; border-left:3px solid #ffb300; padding:8px 12px; font-size:12.5px; color:#6d4c00; margin:8px 0; border-radius:0 4px 4px 0; }
"""


def _fmt(v, suffix="", default="—"):
    if v is None or v == "":
        return f'<span class="dash">{default}</span>'
    return f"{v}{suffix}"


def _sparkline(series, key, width=280, height=46):
    """series: [{date, <key>}] → 内联 SVG 折线（None 留空隙）"""
    if not series:
        return '<span class="dash">—</span>'
    vals = [p.get(key) for p in series]
    nums = [v for v in vals if v is not None]
    if len(nums) < 2:
        return f'<svg class="spark" width="{width}" height="{height}"><line x1="0" y1="{height/2}" x2="{width}" y2="{height/2}" stroke="#ddd" stroke-dasharray="4,3"/></svg>'
    lo, hi = min(nums), max(nums)
    pad = (hi - lo) * 0.15 or max(abs(hi) * 0.05, 1)
    lo -= pad
    hi += pad
    n = len(vals)

    def pt(i, v):
        x = i / max(n - 1, 1) * (width - 8) + 4
        y = height - 6 - (v - lo) / (hi - lo) * (height - 12)
        return f"{x:.1f},{y:.1f}"

    segs, cur = [], []
    for i, v in enumerate(vals):
        if v is None:
            if len(cur) >= 2:
                segs.append(" ".join(cur))
            cur = []
        else:
            cur.append(pt(i, v))
    if len(cur) >= 2:
        segs.append(" ".join(cur))
    polylines = "".join(
        f'<polyline points="{p}" fill="none" stroke="#1a73e8" stroke-width="1.6"/>'
        for p in segs)
    last_i = max(i for i, v in enumerate(vals) if v is not None)
    lx, ly = pt(last_i, vals[last_i]).split(",")
    return (f'<svg class="spark" width="{width}" height="{height}">'
            f'{polylines}<circle cx="{lx}" cy="{ly}" r="2.6" fill="#1a73e8"/>'
            f'<text x="{width-4}" y="{height-2}" font-size="9" fill="#aaa" text-anchor="end">{series[-1]["date"][5:]}</text></svg>')


# ---------------- ① 国家队资金 ----------------
def render_module1(d):
    etf = d.get('etf', {}) or {}
    flows = d.get('flows', {}) or {}
    net = etf.get('netbuy_series', []) or []
    main_series = flows.get('main_flow_series', []) or []
    nb = flows.get('northbound') or {}

    net_today = etf.get('netbuy_today_yi')
    main_today = main_series[-1].get('net_yi') if main_series else None
    main_map = {m['date']: m.get('net_yi') for m in main_series}
    tail = list(reversed(net[-10:]))
    net_color = 'up' if (net_today or 0) > 0 else ('down' if (net_today or 0) < 0 else 'flat')

    rows = "".join(
        f'<tr><td>{p.get("date","")}</td>'
        f'<td class="num">{_fmt(p.get("net_yi"))}</td>'
        f'<td class="num">{_fmt(main_map.get(p.get("date")))}</td></tr>'
        for p in tail)

    return f"""<section class="section">
<h2>① 国家队资金动向 <span style="font-size:12px;color:#90a4ae;font-weight:400">宽基ETF份额净申购（沪市，覆盖{etf.get('netbuy_coverage_pct','—')}%宽基份额）</span></h2>
<div class="sub">{etf.get('netbuy_basis','')}</div>
<div class="flow-grid">
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">宽基ETF今日<b>净申购</b></div>
    <span class="big-num {net_color}">{_fmt(net_today, '亿')}</span><br>{_sparkline(net, 'net_yi')}
    <div style="font-size:10.5px;color:#bbb">份额变化×价格 ≈ 申购赎回净额</div></div>
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">主力资金今日净流入（全市场）</div>
    <span class="big-num {('up' if (main_today or 0) > 0 else 'down')}">{_fmt(main_today, '亿')}</span><br>{_sparkline(main_series, 'net_yi')}
    <div style="font-size:10.5px;color:#bbb">东财「超大单+大单」净额</div></div>
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">宽基ETF今日成交（{etf.get('broad_count','—')}只）</div>
    <span class="big-num">{_fmt(etf.get('broad_today_yi'), '亿')}</span>
    <div style="font-size:10.5px;color:#b26a00;margin-top:6px">⚠️ 含二级市场换手，<b>不等于</b>净申购</div></div>
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">AI相关ETF成交（{etf.get('ai_union_count','—')}只）</div>
    <span class="big-num">{_fmt(etf.get('ai_union_amt_yi'), '亿')}</span>
    <div style="font-size:10.5px;color:#bbb;margin-top:6px">名称含人工智能/算力/芯片/通信等，仅反映交易热度</div></div>
</div>
<div class="table-scroll"><table><thead><tr><th>日期</th><th class="num">宽基ETF净申购(亿)</th><th class="num">主力净流入(亿)</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p style="font-size:11.5px;color:#90a4ae;margin-top:8px">北向单日净买额自 2024-08 起停止披露，已从本模块移除。</p>
</section>"""


# ---------------- ② 财经要闻 ----------------
def render_module2(d):
    news = d.get('news_digest', {}) or {}
    if not news.get('items') and not news.get('themes_points'):
        return ('<section class="section"><h2>② 今日财经要闻</h2>'
                '<p style="color:#90a4ae;font-size:13px">今日 digest 尚未生成（每日 08:30 由 Sina News 管道产出）。</p></section>')
    date_str = news.get('date') or ''
    ds = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else date_str
    src = len(news.get('source_files') or [])
    points = "".join(
        f'<div class="theme-point"><span class="tname">{p["theme"]}</span>{p["point"]}</div>'
        for p in news.get('themes_points', []))
    items = "".join(
        f'<a class="news-item" href="{it["url"]}" target="_blank">• {it["title"]}</a>'
        for it in news.get('items', []))
    return (f'<section class="section"><h2>② 今日财经要闻 '
            f'<span style="font-size:12px;color:#90a4ae;font-weight:400">{ds} · Sina News AI归纳 · {src}个主题</span></h2>'
            f'{points}{items}</section>')


# ---------------- ③ 投资信号 ----------------
def render_module3(d):
    sig = d.get('signals', {}) or {}
    rows = ""
    for s in sig.get('signals', []):
        pct, z = s.get('percentile'), s.get('zscore')
        ref = ""
        if pct is not None:
            ref = f"P{pct}"
        if z is not None:
            ref = (ref + f" · z={z}") if ref else f"z={z}"
        if not ref:
            ref = s.get('baseline') or "—"
        badge = ('<span class="badge b-on">已触发</span>' if s['triggered']
                 else '<span class="badge b-off">未触发</span>')
        direction = (f' <span style="color:#c62828;font-size:12px">{s.get("direction","")}</span>'
                     if s['triggered'] and s.get('direction') else '')
        note = (f'<div style="font-size:11px;color:#90a4ae">{s.get("note","")}</div>'
                if s.get('note') else '')
        rows += (f'<tr><td>{s["name"]}{direction}{note}</td>'
                 f'<td class="num">{_fmt(s.get("current"), s.get("unit",""))}</td>'
                 f'<td class="num" style="color:#888">{ref}</td>'
                 f'<td>{badge}</td></tr>')
    total = len(sig.get('signals', []))
    return (f'<section class="section"><h2>③ 投资信号 '
            f'<span style="font-size:12px;color:#90a4ae;font-weight:400">滚动分位触发 · '
            f'{sig.get("triggered_count",0)}/{total} 已触发</span></h2>'
            f'<table><thead><tr><th>信号</th><th class="num">当前值</th><th class="num">参照</th><th>状态</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<p style="font-size:12px;color:#888;margin-top:8px">{sig.get("push_line","(无触发)") or "(无触发)"}</p>'
            f'<p style="font-size:11.5px;color:#90a4ae">规则：ETF净申购 ≥ 近20日80分位 · 主力净流入 |z|≥1。'
            f'仅当出现新触发时才推送 Telegram（同一信号当天只推一次）。</p></section>')


def render_archive(theme='quant', base='history/'):
    import glob
    files = sorted(glob.glob(os.path.join(HISTORY_DIR, f'{theme}_*.html')), reverse=True)[:30]
    if not files:
        return ""
    items = "".join(
        f'<a class="arc" href="{base}{os.path.basename(f)}">{os.path.basename(f)[len(theme) + 1:-5]}</a>'
        for f in files)
    return (f'<section class="section"><h2>历史归档 '
            f'<span style="font-size:12px;color:#90a4ae;font-weight:400">最近 {len(files)} 期 · 每天一份</span></h2>'
            f'<div class="arc-list">{items}</div></section>')


def render_methodology(d):
    tw = d.get('trading_dates_window', ['—', '—']) or ['—', '—']
    off = (d.get('offline_modules') or {}).get('量化集中度')
    return f"""<section class="section">
<h2>口径说明</h2>
<div class="methodology">
<p><b>国家队资金（v5 已换口径）</b> = 沪市宽基ETF（510050 / 510300 / 510310 / 510330 / 510500 等）<b>份额日变化 × 最新价</b>。
这是汇金/证金申购 ETF 的直接体现。旧版用「ETF成交额」当代理，实测同日成交 147.7亿 而实际净申购仅 0.42亿，差 <b>350 倍</b> ——
成交额绝大部分是做市与套利换手，不是国家队买入，已弃用（成交额仅作背景数字保留）。</p>
<p><b>主力净流入</b> = 东财全市场「超大单 + 大单」净额，按<b>单笔成交金额</b>分类，不代表机构身份；与"国家队"不是同一概念。</p>
<p><b>信号</b> = 滚动分位：ETF净申购 ≥ 近20日80分位 → 国家队大额申购；主力净流入 |z|≥1 → 资金异动。北向停止披露，不计入。</p>
<p><b>数据源</b>：akshare 免费接口（东财 / 上交所 ETF 份额），cron <code>15 16 * * *</code> 盘后刷新。历史窗口 {tw[0]} → {tw[-1]}。</p>
<p><b>局限</b>：仅覆盖沪市宽基（占全市场宽基份额 85%），深市份额无历史接口；估算价用最新价，历史日净申购存在小幅估值误差；
主力净流入按单笔金额分类，无法区分量化/机构/游资。</p>
{f'<p><b>已下线模块：量化集中度</b> —— {off}。</p>' if off else ''}
</div></section>"""


def render(d, theme='quant', base='history/'):
    sig = d.get('signals', {}) or {}
    n_trig = sig.get('triggered_count', 0)
    badge = (f'<span class="sig-badge sig-on">📢 {n_trig}个信号触发</span>' if n_trig
             else '<span class="sig-badge sig-off">无信号触发</span>')
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>投资看板 · {d.get('date','')}</title>
<style>{CSS}</style></head><body>
<div class="container">
<div class="header"><div><h1>📊 个人投资看板</h1><div class="meta">{d.get('date','')} · 生成于 {d.get('generated_at','—')} · v5</div></div>{badge}</div>
{render_module1(d)}
{render_module2(d)}
{render_module3(d)}
{render_archive(theme, base)}
{render_methodology(d)}
<div class="footer">quant-ratio v5 · akshare 免费数据源 · cron 15 16 * * * 盘后刷新</div>
</div></body></html>"""


def main():
    data = load_data()
    if not data:
        print("⚠️ 无数据, 渲染占位页")
        data = {'date': str(date.today()), 'generated_at': '—',
                'etf': {}, 'flows': {}, 'news_digest': {}, 'signals': {},
                'trading_dates_window': ['—', '—']}
    theme = sys.argv[1] if len(sys.argv) > 1 else "quant"
    html = render(data, theme=theme, base='history/')
    with open(os.path.join(DASHBOARD_PATH, "dashboard_investment.html"), 'w') as f:
        f.write(html)
    print(f"✅ 已渲染 dashboard_investment.html ({len(html)} bytes)")

    os.makedirs(HISTORY_DIR, exist_ok=True)
    hist_path = os.path.join(HISTORY_DIR, f"{theme}_{date.today().strftime('%Y-%m-%d')}.html")
    with open(hist_path, 'w') as f:
        f.write(render(data, theme=theme, base=''))
    print(f"✅ 历史归档已渲染 {hist_path}")


if __name__ == "__main__":
    main()
