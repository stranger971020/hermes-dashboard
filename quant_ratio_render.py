#!/usr/bin/env python3
"""quant-ratio 投资看板 HTML 渲染 v5 — 三模块（量化集中度已下线）

页面: dashboard_investment.html + history/quant_YYYY-MM-DD.html
无硬编码 fallback — 数据缺失显示 "—"，不伪造数值。
index.html 是监控页（勿动）。纯 CSS，无 JS。

v5: ① 国家队资金动向 / ② 今日财经要闻 / ③ 投资信号
    模块① 主指标改为 ETF 份额净申购（旧版用成交额，同日差 350 倍）
"""

import glob
import json
import os
import sys
from datetime import date, datetime

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")

# ── V6 日报（trend-trading-system）跨系统数据源 ──
# 契约: v6_daily_report.py 每次日报落 reports/v6_daily/v6_daily_<date>.json (schema=1)
V6_DIR = "/Users/jren/projects/trend-trading-system/reports/v6_daily"
V6_PAGES = "https://stranger971020.github.io/trend-trading-system/reports/v6_daily"
V6_MAX_AGE_DAYS = 5   # 超过则标"数据陈旧"（周末+节假日不产报，5 天覆盖长周末）


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
.flow-box.primary { flex: 1 1 100%; border-color:#a5d6a7; background:#f7fcf7; }
.table-scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
.arc-list { display:flex; flex-wrap:wrap; gap:8px; }
.arc { font-size:12.5px; color:#1a73e8; text-decoration:none; border:1px solid #e3f2fd; border-radius:6px; padding:4px 10px; white-space:nowrap; }
.arc:hover { background:#f3f8ff; }
.warn-note { background:#fff8e1; border-left:3px solid #ffb300; padding:8px 12px; font-size:12.5px; color:#6d4c00; margin:8px 0; border-radius:0 4px 4px 0; }
.nav { display:flex; gap:8px; flex-wrap:wrap; padding:10px 24px; background:#fafbfc; border-bottom:1px solid #eee; }
.nav a { font-size:12.5px; color:#1a73e8; text-decoration:none; border:1px solid #e3f2fd; border-radius:6px; padding:4px 10px; background:#fff; white-space:nowrap; }
.nav a:hover { background:#f3f8ff; }
.nav a.cur { background:#e3f2fd; color:#0d47a1; font-weight:600; border-color:#bbdefb; }
.chip { display:inline-block; font-size:12.5px; border-radius:6px; padding:3px 9px; margin:3px 6px 3px 0; background:#e8f5e9; color:#1b5e20; border:1px solid #c8e6c9; }
.chip.avoid { background:#fafafa; color:#78909c; border-color:#eceff1; }
.chip .r { font-size:11px; opacity:.7; margin-left:4px; }
.kv { font-size:12px; color:#607d8b; margin:2px 0; }
.kv b { color:#263238; }
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
    last = net[-1] if net else {}
    sh_today, sz_today = last.get('sh_yi'), last.get('sz_yi')
    main_today = main_series[-1].get('net_yi') if main_series else None
    main_map = {m['date']: m.get('net_yi') for m in main_series}
    tail = list(reversed(net[-10:]))
    net_color = 'up' if (net_today or 0) > 0 else ('down' if (net_today or 0) < 0 else 'flat')

    rows = "".join(
        f'<tr><td>{p.get("date","")}</td>'
        f'<td class="num">{_fmt(p.get("net_yi"))}</td>'
        f'<td class="num">{_fmt(main_map.get(p.get("date")))}</td></tr>'
        for p in tail)

    sz_txt = f"{sz_today}亿" if sz_today is not None else "次日起追踪"
    return f"""<section class="section">
<h2>① 国家队资金动向 <span style="font-size:12px;color:#90a4ae;font-weight:400">主指标＝宽基ETF份额净申购</span></h2>
<div class="sub">{etf.get('netbuy_basis','')}</div>
<div class="flow-grid">
  <div class="flow-box primary"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">宽基ETF今日<b>净申购</b>（国家队代理）</div>
    <span class="big-num {net_color}">{_fmt(net_today, '亿')}</span><br>{_sparkline(net, 'net_yi')}
    <div style="font-size:11.5px;color:#607d8b;margin-top:4px">沪市 <b>{_fmt(sh_today)}</b>亿 · 深市 <b>{sz_txt}</b></div>
    <div style="font-size:10.5px;color:#bbb">份额变化×价格 ≈ 申购赎回净额；正=申购（资金进场），负=赎回</div></div>
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


# ─────────────────── ④ V6 交易决策（跨系统 · trend-trading-system） ───────────────────

def load_v6_summary():
    """读 trend-trading-system 最新 v6_daily_*.json（schema=1）。

    设计: 只消费结构化摘要，不解析 ~1MB HTML（版式改动不会打断看板）。
    返回 (summary|None, note)；拿不到就如实说明，不伪造数值。
    """
    try:
        files = sorted(glob.glob(os.path.join(V6_DIR, "v6_daily_*.json")))
    except Exception as e:  # noqa: BLE001
        return None, f"V6 目录不可读: {e}"
    if not files:
        return None, ("V6 摘要 JSON 尚未生成 —— v6_daily_report.py 自 2026-11-21 起落 "
                      "reports/v6_daily/v6_daily_YYYYMMDD.json（下一期：工作日 02:45）。")
    try:
        with open(files[-1], encoding="utf-8") as f:
            s = json.load(f)
    except Exception as e:  # noqa: BLE001
        return None, f"V6 摘要解析失败（{os.path.basename(files[-1])}）: {e}"
    if s.get("schema") != 1:
        return None, f"V6 摘要 schema 不匹配: {s.get('schema')}（期望 1）"
    return s, None


def _v6_age_days(s):
    try:
        return (date.today() - datetime.strptime(str(s.get("trading_date")), "%Y%m%d").date()).days
    except Exception:  # noqa: BLE001
        return None


def _v6_pct(v, d=1):
    return f"{v * 100:.{d}f}%" if isinstance(v, (int, float)) else "—"


def _v6_box(label, value, sub="", cls=""):
    sub_html = f'<div style="font-size:10.5px;color:#bbb;margin-top:4px">{sub}</div>' if sub else ""
    return (f'<div class="flow-box"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">{label}</div>'
            f'<span class="big-num {cls}">{value}</span>{sub_html}</div>')


def render_module_v6():
    s, note = load_v6_summary()
    head = ('<section class="section"><h2>④ V6 交易决策 '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            'trend-trading-system · 波段决策单 · 100-500亿域</span></h2>')
    if not s:
        return head + f'<p style="color:#90a4ae;font-size:13px">{note or "无数据"}</p></section>'

    t = s.get("temperature") or {}
    pos = s.get("position") or {}
    risk = s.get("index_risk") or {}
    card = s.get("decision_card") or {}
    team = s.get("national_team") or {}
    emo = s.get("emotion") or {}
    shadow = s.get("shadow") or {}
    mainline = s.get("mainline") or {}
    wave = s.get("wave") or {}
    links = s.get("links") or {}
    bands = pos.get("bands") or {}

    temp_v = t.get("temp")
    lo, hi = bands.get("low"), bands.get("high")
    temp_cls = ""
    if isinstance(temp_v, (int, float)):
        if isinstance(lo, (int, float)) and temp_v < lo:
            temp_cls = "down"        # 低温（优选）→ 绿
        elif isinstance(hi, (int, float)) and temp_v > hi:
            temp_cls = "up"          # 过热 → 红
    temp_box = _v6_box(
        "🌡️ 市场温度",
        (f"{temp_v:.1f}" if isinstance(temp_v, (int, float)) else "—") + "<span style='font-size:13px;color:#90a4ae'>/100</span>",
        f"{t.get('level') or '—'}（温度档：低温<{lo} / 高温>{hi}）", temp_cls)
    pos_box = _v6_box(
        "📐 温度档仓位（历史胜率实证）",
        pos.get("size") or "—",
        f"历史 T+1 胜率 {_v6_pct(pos.get('hist_t1'))} · D+5 {_v6_pct(pos.get('hist_d5'))}")
    risk_box = _v6_box(
        "📊 大盘技术风险",
        risk.get("label") or "—",
        (f"{risk.get('score')}/6 项触发" + (f" · {risk.get('asof')}" if risk.get("asof") else ""))
        if risk else "Index Risk 未计算")
    team_box = _v6_box(
        "🏛️ 国家队进出场信号",
        team.get("verdict") or "—",
        f"置信度 {team.get('confidence') or '—'}" if team else "数据不足")
    emo_box = _v6_box(
        "🎭 情绪周期",
        (f"{emo.get('emo_score')}" if emo.get("emo_score") is not None else "—") + "<span style='font-size:13px;color:#90a4ae'>/100</span>",
        (f"{emo.get('phase') or '—'} · 涨停 {emo.get('limit_up')} / 跌停 {emo.get('limit_down')}")
        if emo else "未计算")
    shadow_box = _v6_box(
        "📈 影子实盘（前向跟踪）",
        (f"T+1 {_v6_pct(shadow.get('t1_wr'))}" if shadow.get("t1_wr") is not None else "—"),
        (f"已结算 {shadow.get('n_settled_t1')} 笔 · 累计 {shadow.get('cum_t1')}%")
        if shadow.get("n_settled_t1") else "样本不足")

    # ── 决策单（阶段自适应行业臂）──
    buy = card.get("industries_buy") or []
    avoid = card.get("industries_avoid") or []
    buy_chips = "".join(
        f'<span class="chip">{x.get("ind")}<span class="r">热度分位 {x.get("rank")} · 20日 {x.get("r20")}%</span></span>'
        for x in buy) or '<span style="font-size:12.5px;color:#90a4ae">本期行业臂 = 全市场择优（该阶段行业筛选无增益）</span>'
    avoid_chips = "".join(
        f'<span class="chip avoid">{x.get("ind")}<span class="r">分位 {x.get("rank")}</span></span>'
        for x in avoid) or '<span style="font-size:12.5px;color:#90a4ae">无</span>'
    arm = card.get("arm") or "—"
    arm_disp = "all（全市场择优）" if arm == "all" else str(arm)
    card_html = f"""
<div class="flow-grid" style="margin-top:14px">
  <div class="flow-box primary">
    <div style="font-size:12px;color:#607d8b;margin-bottom:6px">🎯 波段决策单（{card.get('asof') or s.get('trading_date')}）</div>
    <div class="kv">阶段 <b>{card.get('regime') or t.get('regime') or '—'}</b> · 阶段仓位 <b>{card.get('position') or pos.get('size') or '—'}</b> · 行业臂 <b>{arm_disp}</b></div>
    <div class="kv">市场广度 <b>{_v6_pct(card.get('breadth'), 0)}</b> · 沪深300 250日回撤 <b>{_v6_pct(card.get('dd250'))}</b>
      {(' · ' + str(card.get('_tilt_note'))) if card.get('_tilt_note') else ''}</div>
    <div style="margin-top:10px;font-size:12px;color:#607d8b">买入行业</div>
    <div style="margin-top:2px">{buy_chips}</div>
    <div style="margin-top:8px;font-size:12px;color:#607d8b">回避（热度分位 ≥90 · 最热 10%）</div>
    <div style="margin-top:2px">{avoid_chips}</div>
    <div style="font-size:11px;color:#90a4ae;margin-top:8px">{card.get('arm_note') or ''}</div>
  </div>
</div>"""

    # ── 个股候选 ──
    stocks = (card.get("stocks") or [])[:8]

    def _n(v, d=1, suffix=""):
        return f"{v:.{d}f}{suffix}" if isinstance(v, (int, float)) else "—"

    if stocks:
        rows = "".join(
            f'<tr><td>{x.get("name") or "—"}</td><td style="color:#90a4ae">{x.get("ts_code")}</td>'
            f'<td>{x.get("ind") or "—"}</td>'
            f'<td class="num">{_n(x.get("score"))}</td>'
            f'<td class="num">{_n(x.get("p_win"), 1, "%")}</td>'
            f'<td class="num {"up" if (x.get("expect") or 0) > 0 else "down"}">{_n(x.get("expect"), 1, "%")}</td>'
            f'<td class="num">{_n(x.get("mv"), 0, "亿")}</td>'
            f'<td class="num">{_n(abs(x["dd250"]) if isinstance(x.get("dd250"), (int, float)) else None, 1, "%")}</td>'
            f'<td class="num">{_n(x.get("vol20"), 2, "%")}</td></tr>'
            for x in stocks)
        stock_html = f"""
<div class="table-scroll" style="margin-top:6px">
<table><thead><tr><th>名称</th><th>代码</th><th>行业</th><th class="num">波段分</th><th class="num">P(Win)</th>
<th class="num">预期涨幅</th><th class="num">市值</th><th class="num">250日回撤</th><th class="num">20日波动</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div style="font-size:11px;color:#90a4ae;margin-top:4px">模型 {card.get('model') or '—'} · 按 swing_score 排序 · 250日回撤取绝对值（越深=跌得越多）· 仅供研究, 非投资建议</div>"""
    else:
        stock_html = '<p style="font-size:12.5px;color:#90a4ae;margin:6px 0">本期无个股候选（行业臂或模型未就绪）。</p>'

    # ── 主线 / 主升浪 一行速览 ──
    ml = mainline.get("ranking") or []
    ml_txt = " · ".join(f"{x.get('ind')}({x.get('ml20')})" for x in ml[:5]) or "—"
    ws = wave.get("states") or []
    wave_txt = " · ".join(f"{x.get('name')}={x.get('status')}" for x in ws) or "—"
    glance_html = (f'<div class="kv" style="margin-top:10px">🎯 主线雷达 L2 top5: <b>{ml_txt}</b></div>'
                   f'<div class="kv">🌊 主升浪雷达: <b>{wave_txt}</b></div>')

    # ── 数据时点 / 陈旧警示 / 外链 ──
    age = _v6_age_days(s)
    stale_html = ""
    if age is not None and age > V6_MAX_AGE_DAYS:
        stale_html = (f'<div class="warn-note">⚠️ V6 数据截至 <b>{s.get("trading_date")}</b>（{age} 天前）。'
                      f'下一期 {s.get("next_date") or "—"} —— 工作日 02:45 出报，周末/节假日不产。</div>')
    if s.get("stale_days"):
        stale_html += (f'<div class="warn-note">⚠️ V6 日报自报数据滞后 {s["stale_days"]} 个交易日'
                       f'（行情截至 {s.get("price_date")}），指标仅供参考。</div>')
    html_link = links.get("html") or (f"{V6_PAGES}/v6_daily_{s.get('trading_date')}.html")
    meta_html = (f'<div style="margin-top:10px;font-size:12px;color:#607d8b;line-height:1.8">'
                 f'数据日期 <b>{s.get("trading_date") or "—"}</b>（下一期 {s.get("next_date") or "—"}）'
                 f' · 生成 {s.get("generated_at") or "—"} · 模型 {s.get("model_ver") or "—"}<br>'
                 f'<a class="news-item" style="display:inline" href="{html_link}" target="_blank" rel="noopener">'
                 f'→ V6 完整日报（12 段：风险/预案/主升浪/主线/国家队/温度/情绪/衰减榜/仓位规则/明日策略/影子归因）</a></div>')

    return (head + stale_html
            + '<div class="flow-grid">' + temp_box + pos_box + risk_box + '</div>'
            + '<div class="flow-grid">' + team_box + emo_box + shadow_box + '</div>'
            + card_html + stock_html + glance_html + meta_html + '</section>')


def render_nav():
    """跨看板导航 —— 投资相关内容分散在 3 个入口，这里给出唯一入口。"""
    items = [
        ("个人投资看板", "dashboard_investment.html", True),
        ("V6 交易日报", f"{V6_PAGES}/v6_daily_{date.today().strftime('%Y%m%d')}.html", False),
        ("政策信号", "policy/index.html", False),
        ("财经早报归档", "news/index.html", False),
        ("系统健康", "health/index.html", False),
        ("系统监控", "index.html", False),
    ]
    links = "".join(f'<a class="{"cur" if cur else ""}" href="{href}">{name}</a>'
                    for name, href, cur in items)
    return f'<div class="nav">{links}</div>'


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
<p><b>局限</b>：沪市份额可取历史（交易所按日），深市无历史接口 —— 自 {d.get('etf', {}).get('sz_tracked_since', '—')} 起逐日快照累积（已 {d.get('etf', {}).get('sz_tracked_days', 0)} 日），
首日深市无变化值；估算价用最新价，历史日净申购存在小幅估值误差；主力净流入按单笔金额分类，无法区分量化/机构/游资。</p>
<p><b>④ V6 交易决策</b> = 从 trend-trading-system 的 V6 日报结构化摘要（<code>reports/v6_daily/v6_daily_YYYYMMDD.json</code>，schema=1）读取，只做展示，不在本看板重算。
口径分工：本看板 ① 是<b>资金来源层</b>（ETF 份额净申购 / 主力净额，原始事实），④ 的「国家队进出场信号」是<b>信号判定层</b>（份额×点位×杠杆交叉验证 → ENTRY/EXIT/ROTATE，含置信度）；
两者数据源相同、口径不同，出现方向不一致时以「① 看资金事实、④ 看判定结论」分别理解，不要互相校验。</p>
<p><b>局限（④）</b>：V6 只在工作日 02:45 出报，周末/节假日显示上一交易日；看板不缓存、不重算，V6 摘要缺失即显示「尚未生成」，不填占位数值。</p>
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
<div class="header"><div><h1>📊 个人投资看板</h1><div class="meta">{d.get('date','')} · 生成于 {d.get('generated_at','—')} · v6（含 V6 决策层）</div></div>{badge}</div>
{render_nav()}
{render_module1(d)}
{render_module2(d)}
{render_module3(d)}
{render_module_v6()}
{render_archive(theme, base)}
{render_methodology(d)}
<div class="footer">quant-ratio v6 · akshare 免费数据源 + V6 日报（工作日 02:45）· cron 15 16 * * * 盘后刷新</div>
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
