#!/usr/bin/env python3
"""quant-ratio 投资看板 HTML 渲染 v2（匹配 quant_data.json 新结构）

页面: dashboard_investment.html + history/{theme}_{date}.html
无硬编码 fallback — 数据缺失显示 "—"，不伪造数值。
index.html 是监控页（勿动），投资看板独立成文件。
"""

import json
import os
from datetime import date

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")  # v2: 独立数据文件


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
.container { max-width: 960px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }
.header { background: #1a1a2e; color: white; padding: 20px 24px; }
.header h1 { margin: 0 0 6px 0; font-size: 20px; }
.header .meta { font-size: 13px; opacity: 0.75; }
.section { padding: 20px 24px; border-bottom: 1px solid #eee; }
.section h2 { font-size: 16px; margin: 0 0 14px 0; color: #333; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #f0f0f0; }
th { background: #fafafa; color: #666; font-weight: 600; white-space: nowrap; }
tr:hover td { background: #f9f9ff; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.badge-high { background: #e8f5e9; color: #2e7d32; }
.badge-mid { background: #fff8e1; color: #f57f17; }
.badge-low { background: #fafafa; color: #757575; }
input.tab-radio { display: none; }
.tab-labels { display: flex; gap: 4px; margin-bottom: 0; border-bottom: 2px solid #e8e8e8; }
.tab-labels label { display: inline-block; padding: 8px 16px; cursor: pointer; background: #f5f5f5; font-size: 13px; color: #666; border-radius: 6px 6px 0 0; margin-right: 2px; }
.panes .tab-pane { display: none; padding: 14px 4px; }
#tab-0:checked ~ .tab-labels label[for="tab-0"],
#tab-1:checked ~ .tab-labels label[for="tab-1"],
#tab-2:checked ~ .tab-labels label[for="tab-2"],
#tab-3:checked ~ .tab-labels label[for="tab-3"] { background: white; color: #1a73e8; font-weight: 600; }
#tab-0:checked ~ .panes #pane-0,
#tab-1:checked ~ .panes #pane-1,
#tab-2:checked ~ .panes #pane-2,
#tab-3:checked ~ .panes #pane-3 { display: block; }
.methodology { font-size: 12px; color: #888; line-height: 1.7; }
.methodology code { background: #f5f5f5; padding: 1px 4px; border-radius: 3px; }
.footer { padding: 16px 24px; font-size: 12px; color: #999; text-align: center; }
.dash { color: #bbb; }
"""


def _fmt(v, suffix="", default="—"):
    if v is None or v == "":
        return f'<span class="dash">{default}</span>'
    return f"{v}{suffix}"


def render_sector_table(sectors):
    rows = ""
    for s in sorted(sectors, key=lambda x: x.get('综合评分', 0), reverse=True):
        rating = s.get('评级', '')
        badge_cls = {'高': 'badge-high', '中': 'badge-mid'}.get(rating, 'badge-low')
        rows += f"""<tr>
            <td><strong>{s['板块']}</strong></td>
            <td class="num">{_fmt(s.get('成分股数'), '只')}</td>
            <td class="num">{_fmt(s.get('量化活跃占比%'), '%')} ({_fmt(s.get('量化活跃股数'), '只')})</td>
            <td class="num">{_fmt(s.get('板块换手率%'), '%')}</td>
            <td>{s.get('趋势', '—')} {_fmt(s.get('换手变化%'), '%')}</td>
            <td class="num">{_fmt(s.get('ETF数'), '只')} / {_fmt(s.get('ETF成交亿'), '亿')}</td>
            <td class="num"><strong>{_fmt(s.get('综合评分'))}</strong></td>
            <td><span class="badge {badge_cls}">{rating or '—'}</span></td>
        </tr>"""
    return f"""<div class="section">
        <h2>① 量化集中度排名（AI相关板块）</h2>
        <table>
            <thead><tr><th>板块</th><th class="num">成分股</th><th class="num">量化活跃占比</th><th class="num">板块换手率(3日均)</th><th>趋势</th><th class="num">相关ETF/成交</th><th class="num">综合评分</th><th>评级</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def render_trend_table(sectors):
    # 近5日：日期 × 板块 的换手率透视表（hist_em 真实历史）
    dates = []
    for s in sectors:
        for d in s.get('近5日', []):
            if d['日期'] not in dates:
                dates.append(d['日期'])
    dates.sort()

    if not dates:
        return ""

    head = "".join(f'<th class="num">{d[-5:]}</th>' for d in dates)
    rows = ""
    for s in sorted(sectors, key=lambda x: x.get('综合评分', 0), reverse=True):
        by_date = {d['日期']: d for d in s.get('近5日', [])}
        cells = "".join(f'<td class="num">{by_date[d]["换手率%"]}%</td>' if d in by_date else '<td class="num"><span class="dash">—</span></td>' for d in dates)
        rows += f"<tr><td><strong>{s['板块']}</strong></td>{cells}</tr>"

    return f"""<div class="section">
        <h2>② 近5日换手率趋势（%）</h2>
        <table>
            <thead><tr><th>板块</th>{head}</tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def render_stock_tabs(sectors):
    tabs, panes = [], []
    ordered = sorted(sectors, key=lambda x: x.get('综合评分', 0), reverse=True)
    for i, s in enumerate(ordered):
        checked = "checked" if i == 0 else ""
        tabs.append(f'<input class="tab-radio" type="radio" name="stk" id="tab-{i}" {checked}><label for="tab-{i}">{s["板块"]}</label>')

        def stock_table(rows, title):
            body = "".join(
                f"<tr><td>{r['名称']} <span style='color:#aaa;font-size:11px'>{r['代码']}</span></td>"
                f"<td class='num'><strong>{r['换手率%']}%</strong></td>"
                f"<td class='num'>{r['成交额亿']}亿</td></tr>"
                for r in rows)
            return (f"<h3 style='font-size:14px;margin:8px 0'>🔥 {title}</h3>"
                    f"<table><thead><tr><th>个股</th><th class='num'>换手率</th><th class='num'>成交额</th></tr></thead>"
                    f"<tbody>{body or '<tr><td colspan=3><span class=dash>—</span></td></tr>'}</tbody></table>")

        panes.append(f"""<div class="tab-pane" id="pane-{i}">
            {stock_table(s.get('top5_stocks', []), 'Top 5 最活跃（当日换手率）')}
            {stock_table(s.get('bottom5_stocks', []), 'Bottom 5 低活跃参考')}
        </div>""")

    return f"""<div class="section">
        <h2>③ 个股活跃度明细</h2>
        {''.join(tabs)}
        <div class="tab-labels"></div>
        <div class="panes">{''.join(panes)}</div>
    </div>"""


def render_methodology(data):
    thr = data.get('active_threshold_pct', 3.0)
    return f"""<div class="section">
        <h2>口径说明</h2>
        <div class="methodology">
            • <strong>量化活跃股占比</strong> = 成分股中当日换手率 &gt; {thr}% 的个股比例（数据源: stock_board_industry_cons_em，东方财富）<br>
            • <strong>板块换手率(3日均)</strong> = 近3个交易日均值；趋势 = 近3日均 vs 前3日均变化（数据源: stock_board_industry_hist_em 真实历史）<br>
            • <strong>相关ETF</strong> = ETF名称含板块关键词的基金数与当日成交额合计（计算机=人工智能/AI/算力/大数据/云计算/软件；电子=芯片；通信设备=通信/5G；半导体=半导体），按代码去重<br>
            • <strong>综合评分(0-100)</strong> = 40×min(1,活跃占比/35%) + 30×min(1,板块换手率/5%) + 20×min(1,ETF成交亿/20) + 10×min(1,ETF数/30)<br>
            • <strong>评级</strong>: ≥60 高 / 40-60 中 / &lt;40 低<br>
            • 综合评分为透明公式的代理指标（非直接持仓数据），用于板块间横向对比，绝对值参考意义有限
        </div>
    </div>"""


def generate_html(data):
    sectors = data.get('sectors', [])

    if not sectors:
        body = '<div class="section"><h2>⚠️ 暂无数据</h2><p style="color:#888;font-size:13px">quant_data.json 未生成或为空。请运行 collect_quant_ratio.py。</p></div>'
    else:
        body = (render_sector_table(sectors) + render_trend_table(sectors)
                + render_stock_tabs(sectors) + render_methodology(data))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>我的投资看板 · 量化集中度</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 我的投资看板 · AI相关板块量化集中度</h1>
        <div class="meta">数据日期: {data.get('date', '—')} | 生成时间: {data.get('generated_at', '—')} | 日更（每日16:15盘后）| AI相关ETF(去重): {_fmt(data.get('ai_etf_union_count'), '只')}</div>
    </div>
    {body}
    <div class="footer">quant-ratio v2 · 数据源: akshare (东方财富) · 所有数值为真实接口返回值</div>
</div>
</body>
</html>"""


def render_main_index(data):
    """投资看板主页面 → dashboard_investment.html（index.html 是监控页，勿动）"""
    html = generate_html(data)
    filepath = os.path.join(DASHBOARD_PATH, "dashboard_investment.html")
    with open(filepath, 'w') as f:
        f.write(html)
    print(f"✅ 投资看板已渲染至 {filepath} ({os.path.getsize(filepath)} bytes)")


def render_history(data, theme="quant"):
    """历史归档 → history/{theme}_{date}.html"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    html = generate_html(data)
    filename = f"{theme}_{date.today().strftime('%Y-%m-%d')}.html"
    filepath = os.path.join(HISTORY_DIR, filename)
    with open(filepath, 'w') as f:
        f.write(html)
    print(f"✅ 历史归档已渲染至 {filepath}")


if __name__ == "__main__":
    data = load_data()
    render_main_index(data)
    import sys
    theme = sys.argv[1] if len(sys.argv) > 1 else "quant"
    render_history(data, theme=theme)
