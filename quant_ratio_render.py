#!/usr/bin/env python3
"""quant-ratio 投资看板 HTML 渲染 v3 — 匹配 collect v3 数据（四模块）

页面: dashboard_investment.html + history/{theme}_{date}.html
无硬编码 fallback — 数据缺失显示 "—"，不伪造数值。
index.html 是监控页（勿动），投资看板独立成文件。
纯 CSS Tab（无JS依赖）。趋势图用内联 SVG sparkline。
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
.sig-on { background:#e8f5e9; color:#fff; } .sig-off { background:#eceff1; color:#607d8b; }
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
input.tab-radio { display: none; }
.tabs { border-bottom: 2px solid #e8e8e8; margin-bottom: 14px; display:flex; gap:4px; flex-wrap:wrap; }
.tabs label { padding: 8px 16px; cursor: pointer; background:#f5f5f5; font-size:13px; color:#666; border-radius:6px 6px 0 0; }
.pane { display:none; }
#tab-a:checked ~ .tabs label[for="tab-a"], #tab-b:checked ~ .tabs label[for="tab-b"], #tab-c:checked ~ .tabs label[for="tab-c"] { background:white; color:#1a73e8; font-weight:600; border-bottom:2px solid #1a73e8; }
#tab-a:checked ~ .panes #pane-a, #tab-b:checked ~ .panes #pane-b, #tab-c:checked ~ .panes #pane-c { display:block; }
.sector-card { border:1px solid #eee; border-radius:8px; padding:12px 16px; margin-bottom:14px; }
.sector-head { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
.sector-name { font-size:15px; font-weight:700; color:#263238; }
.kpi-row { display:flex; gap:18px; flex-wrap:wrap; font-size:12.5px; color:#455a64; margin-bottom:8px; }
.kpi b { font-size:14px; color:#263238; }
.spark { vertical-align:middle; }
.stock-sub { width:100%; border-collapse:collapse; font-size:12.5px; margin-top:6px; }
.stock-sub th, .stock-sub td { padding:4px 8px; border-bottom:1px solid #f5f5f5; }
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
"""


def _fmt(v, suffix="", default="—"):
    if v is None or v == "":
        return f'<span class="dash">{default}</span>'
    return f"{v}{suffix}"


def _delta_html(delta):
    if delta is None:
        return '<span class="flat">—</span>'
    cls = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    sign = "+" if delta > 0 else ""
    return f'<span class="{cls}">{sign}{delta}</span>'


def _trend_icon(t):
    m = {'📈': 'up', '📉': 'down'}
    cls = m.get(t, 'flat')
    return f'<span class="{cls}">{t or "→"}</span>'


def _sparkline(series, width=280, height=46):
    """series: [{date, active_ratio_pct|amt_yi}] → 内联SVG折线 (None值留空隙)"""
    if not series:
        return '<span class="dash">—</span>'
    vals = [p.get('active_ratio_pct', p.get('amt_yi', p.get('net_yi'))) for p in series]
    nums = [v for v in vals if v is not None]
    if not nums:
        return f'<svg class="spark" width="{width}" height="{height}"><line x1="0" y1="{height/2}" x2="{width}" y2="{height/2}" stroke="#ddd" stroke-dasharray="4,3"/></svg>'
    lo, hi = min(nums), max(nums)
    pad = (hi - lo) * 0.15 or max(abs(hi) * 0.05, 1)
    lo -= pad; hi += pad
    n = len(vals)

    def pt(i, v):
        x = i / max(n - 1, 1) * (width - 8) + 4
        y = height - 6 - (v - lo) / (hi - lo) * (height - 12)
        return f"{x:.1f},{y:.1f}"

    segs = []
    cur = []
    for i, v in enumerate(vals):
        if v is None:
            if len(cur) >= 2:
                segs.append(" ".join(cur))
            cur = []
        else:
            cur.append(pt(i, v))
    if len(cur) >= 2:
        segs.append(" ".join(cur))
    polylines = "".join(f'<polyline points="{p}" fill="none" stroke="#1a73e8" stroke-width="1.6"/>' for p in segs)
    last_i = max(i for i, v in enumerate(vals) if v is not None)
    lx, ly = pt(last_i, vals[last_i]).split(",")
    dot = f'<circle cx="{lx}" cy="{ly}" r="2.6" fill="#1a73e8"/>'
    return (f'<svg class="spark" width="{width}" height="{height}">'
            f'{polylines}{dot}'
            f'<text x="{width-4}" y="{height-2}" font-size="9" fill="#aaa" text-anchor="end">{series[-1]["date"][5:]}</text></svg>')


def _stock_table(rows, title):
    if not rows:
        return ""
    trs = ""
    for r in rows:
        trs += (f'<tr><td>{r.get("名称","")}</td>'
                f'<td class="num">{_fmt(r.get("换手率%"), "%")}</td>'
                f'<td class="num">{_fmt(r.get("prev_day"), "%")}</td>'
                f'<td class="num">{_fmt(r.get("avg_5d"), "%")}</td>'
                f'<td class="num">{_fmt(r.get("avg_10d"), "%")}</td>'
                f'<td class="num">{_fmt(r.get("avg_20d"), "%")}</td></tr>')
    return (f'<div style="margin-top:8px"><strong style="font-size:12.5px;color:#607d8b">{title}</strong>'
            f'<table class="stock-sub"><thead><tr><th>个股</th><th class="num">今日换手%</th>'
            f'<th class="num">上一交易日</th><th class="num">近5日均值</th><th class="num">近10日均值</th>'
            f'<th class="num">近20日均值</th></tr></thead><tbody>{trs}</tbody></table></div>')


def _sector_card(s):
    etf_html = ""
    if s.get('相关ETF数') is not None:
        etf_html = f'<span>相关ETF <b>{s["相关ETF数"]}</b>只 / {_fmt(s.get("相关ETF成交亿"), "亿")}</span>'
    return f"""
<div class="sector-card">
  <div class="sector-head">
    <span class="sector-name">{s['板块']} <span style="font-weight:400;color:#90a4ae;font-size:12px">成分股 {s.get('成分股数','—')}只</span></span>
    <div>{_trend_icon(s.get('趋势'))} {_sparkline(s.get('历史序列', []))}</div>
  </div>
  <div class="kpi-row">
    <span>量化集中度(今日) <b>{_fmt(s.get('量化集中度%_今日'), '%')}</b></span>
    <span>上一交易日 {_fmt(s.get('量化集中度%_上一交易日'), '%')} (环比 {_delta_html(s.get('环比变化pct'))} pct)</span>
    <span>近5日均值 <b>{_fmt(s.get('近5日均值%'), '%')}</b></span>
    <span>近10日均值 {_fmt(s.get('近10日均值%'), '%')}</span>
    <span>近20日均值 {_fmt(s.get('近20日均值%'), '%')}</span>
    {etf_html}
  </div>
  {_stock_table(s.get('top5_stocks', []), 'Top5 高活跃个股（按今日换手率）')}
  {_stock_table(s.get('bottom5_stocks', []), 'Bottom5 低活跃个股')}
</div>"""


def render_module1(d):
    groups = [
        ('tab-a', 'pane-a', 'AI相关板块', d.get('ai_sectors', [])),
        ('tab-b', 'pane-b', 'Top10 高量化集中板块', d.get('top10_sectors', [])),
        ('tab-c', 'pane-c', 'Bottom10 低量化集中板块', d.get('bottom10_sectors', [])),
    ]
    tabs = "".join(f'<label for="{tid}">{label}</label>' for tid, _, label, _ in groups)
    panes = ""
    for tid, pid, label, blocks in groups:
        cards = "".join(_sector_card(s) for s in blocks) if blocks else '<p class="dash">数据待更新</p>'
        panes += f'<div id="{pid}" class="pane">{cards}</div>'
    return (f"""<section class="section">
<h2>① 量化集中度 <span style="font-size:12px;color:#90a4ae;font-weight:400">（成分股换手率&gt;{d.get('active_threshold_pct',3)}%占比 · 历史趋势为该指标本身）</span></h2>
<div class="sub">排名门槛：成分股≥{d.get('min_sector_stocks','—')}只 | 全市场 {d.get('sector_universe_size','—')} 个行业板块参与扫描</div>
<input class="tab-radio" type="radio" name="mtab" id="tab-a" checked><input class="tab-radio" type="radio" name="mtab" id="tab-b"><input class="tab-radio" type="radio" name="mtab" id="tab-c">
<div class="tabs">{tabs}</div>
<div class="panes">{panes}</div>
</section>""")


def render_module2(d):
    etf = d.get('etf', {}) or {}
    flows = d.get('flows', {}) or {}
    broad_series = etf.get('broad_series', [])
    main_series = flows.get('main_flow_series', [])
    nb = flows.get('northbound') or {}

    def _big(series, key):
        return series[-1].get(key) if series else None

    nb_note = f' <span style="font-size:11px;color:#90a4ae">({nb.get("note","")})</span>' if nb and nb.get('note') else ''
    tail_b = list(reversed(broad_series[-10:]))
    tail_m = {m['date']: m for m in main_series}

    return f"""<section class="section">
<h2>② 国家队资金动向</h2>
<div class="sub">宽基ETF成交额 = 国家队入市代理指标（沪深300/上证50/中证500 ETF，去重后按成交额Top40口径）</div>
<div class="flow-grid">
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">宽基ETF今日成交（全口径 {etf.get('broad_count','—')}只）</div>
    <span class="big-num">{_fmt(etf.get('broad_today_yi'), '亿')}</span><br>{_sparkline(broad_series)}<span style="font-size:10px;color:#bbb">趋势图=Top40口径</span></div>
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">主力资金今日净流入</div>
    <span class="big-num">{_fmt(_big(main_series, 'net_yi'), '亿')}</span><br>{_sparkline(main_series)}</div>
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">北向今日净买额（{nb.get('date','—')}）</div>
    <span class="big-num">{_fmt(nb.get('netbuy_yi'), '亿')}</span>{nb_note}</div>
  <div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">AI相关ETF（union）</div>
    <span class="big-num">{_fmt(etf.get('ai_union_amt_yi'), '亿')}</span><br>
    <span style="font-size:12px;color:#90a4ae">{etf.get('ai_union_count','—')}只（去重）</span></div>
</div>
<table><thead><tr><th>日期</th><th class="num">宽基ETF成交(亿)</th><th class="num">主力净流入(亿)</th></tr></thead><tbody>""" + "".join(
        f'<tr><td>{b.get("date","")}</td><td class="num">{_fmt(b.get("amt_yi"))}</td>'
        f'<td class="num">{_fmt(tail_m[b["date"]]["net_yi"]) if b["date"] in tail_m else "—"}</td></tr>'
        for b in tail_b) + """</tbody></table>
</section>"""


def render_module3(d):
    news = d.get('news_digest', {}) or {}
    if not news.get('items') and not news.get('themes_points'):
        return ('<section class="section"><h2>③ 今日财经要闻</h2>'
                '<p style="color:#90a4ae;font-size:13px">今日 digest 尚未生成（每日 08:30 由 Sina News 管道产出）。</p></section>')
    date_str = news.get('date') or ''
    ds = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else date_str
    points = "".join(f'<div class="theme-point"><span class="tname">{p["theme"]}</span>{p["point"]}</div>' for p in news.get('themes_points', []))
    items = "".join(f'<a class="news-item" href="{it["url"]}" target="_blank">• {it["title"]}</a>' for it in news.get('items', []))
    return (f'<section class="section"><h2>③ 今日财经要闻 <span style="font-size:12px;color:#90a4ae;font-weight:400">{ds} · Sina News AI归纳</span></h2>'
            f'{points}{items}</section>')


def render_module4(d):
    sig = d.get('signals', {}) or {}
    rows = ""
    for s in sig.get('signals', []):
        badge = '<span class="badge b-on">已触发</span>' if s['triggered'] else '<span class="badge b-off">未触发</span>'
        direction = f' <span style="color:#c62828;font-size:12px">{s.get("direction","")}</span>' if s['triggered'] and s.get('direction') else ''
        rows += (f'<tr><td>{s["name"]}{direction}</td>'
                 f'<td class="num">{_fmt(s.get("current"), s.get("unit",""))}</td>'
                 f'<td class="num" style="color:#888">阈值 {s.get("threshold")}{s.get("unit","")}</td>'
                 f'<td>{badge}</td></tr>')
    return (f'<section class="section"><h2>④ 投资信号 <span style="font-size:12px;color:#90a4ae;font-weight:400">阈值触发制 · '
            f'{sig.get("triggered_count",0)}/{len(sig.get("signals",[]))} 已触发</span></h2>'
            f'<table><thead><tr><th>信号</th><th class="num">当前值</th><th class="num">阈值</th><th>状态</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<p style="font-size:12px;color:#888;margin-top:8px">{sig.get("push_line","(无触发)")}</p></section>')


def render_methodology(d):
    tw = d.get('trading_dates_window', ['—', '—']) or ['—', '—']
    return f"""<section class="section">
<h2>口径说明</h2>
<div class="methodology">
<p><b>量化集中度（板块）</b> = 成分股中当日换手率 &gt; {d.get('active_threshold_pct',3)}% 的个股占比。换手率是量化资金高频交易的公开代理指标（skill标准阈值，非精确识别）。</p>
<p><b>历史趋势</b> = 上述集中度指标在历史交易日的逐日值 + 近5/10/20日均值对比；成分股名单取当前口径（历史成员变动为近似）。</p>
<p><b>全市场排名</b> = {d.get('sector_universe_size','—')}个行业板块中成分股≥{d.get('min_sector_stocks','—')}只者参与，按今日集中度排序取 Top10/Bottom10。</p>
<p><b>国家队资金</b> = 宽基ETF（沪深300/上证50/中证500）成交合计为入市代理；主力资金来自全市场主力净流入；北向为参考值（披露规则调整后部分日期净买额为0）。</p>
<p><b>数据源</b>：东方财富公开接口（akshare免费源），盘后 16:15 自动刷新。历史窗口 {tw[0]} → {tw[-1]}。</p>
<p><b>局限</b>：换手率代理无法区分量化/非量化资金；ETF成交额含机构与散户；个股历史为逐股回填（新板块首日可能缺早期点）。</p>
</div></section>"""


def render(d):
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
<div class="header"><div><h1>📊 个人投资看板</h1><div class="meta">{d.get('date','')} · 生成于 {d.get('generated_at','—')}</div></div>{badge}</div>
{render_module1(d)}
{render_module2(d)}
{render_module3(d)}
{render_module4(d)}
{render_methodology(d)}
<div class="footer">quant-ratio v3 · akshare 免费数据源 · 盘后自动刷新</div>
</div></body></html>"""


def main():
    data = load_data()
    if not data:
        print("⚠️ 无数据, 渲染占位页")
        data = {'date': str(date.today()), 'generated_at': '—', 'active_threshold_pct': 3.0,
                'min_sector_stocks': 20, 'sector_universe_size': None,
                'ai_sectors': [], 'top10_sectors': [], 'bottom10_sectors': [],
                'etf': {}, 'flows': {}, 'news_digest': {}, 'signals': {}}
    html = render(data)
    with open(os.path.join(DASHBOARD_PATH, "dashboard_investment.html"), 'w') as f:
        f.write(html)
    print(f"✅ 已渲染 dashboard_investment.html ({len(html)} bytes)")

    os.makedirs(HISTORY_DIR, exist_ok=True)
    theme = sys.argv[1] if len(sys.argv) > 1 else "quant"
    hist_path = os.path.join(HISTORY_DIR, f"{theme}_{date.today().strftime('%Y-%m-%d')}.html")
    with open(hist_path, 'w') as f:
        f.write(html)
    print(f"✅ 历史归档已渲染 {hist_path}")


if __name__ == "__main__":
    main()
