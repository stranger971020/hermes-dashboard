#!/usr/bin/env python3
"""quant-ratio 投资看板 HTML 渲染 v5 — 三模块（量化集中度已下线）

页面: dashboard_investment.html + history/quant_YYYY-MM-DD.html
无硬编码 fallback — 数据缺失显示 "—"，不伪造数值。
index.html 是监控页（勿动）。纯 CSS；⑦ 段含一个移动止损计算器（inline JS）。

v5: ① 国家队资金动向 / ② 今日财经要闻 / ③ 投资信号
    模块① 主指标改为 ETF 份额净申购（旧版用成交额，同日差 350 倍）

v5.4 (2026-11-27): ⑦ 段出场口径统一（读 trading-journal/config/exit_rules.json）
- 移动止盈激活 = 峰值浮盈曾达 +10%；峰值收盘回撤 8% → 清仓剩余
- 止损 = 成本 -8% 减半 / -13% 清仓；删除「8% 移动止损」模糊说法
- 计算器新增「成本价」输入，可判断是否已激活（此前只算回撤线）

v5.3 (2026-11-24): 情绪标签可读性
- 「上升期」→「高潮期」：原词易被读成趋势向上，实际是 score>=65 的过热桶。
- 新增 phase_detail「单日爆发 / 连续高潮」；④/⑥ 段追加显示，缺字段自动降级。
- ⑥ 段判读规则补一行：>=65 高潮/过热（反向），当日分-5日均值>=25 标单日爆发。

v5.2 (2026-11-23): 各模块标题行增加「刷新 MM-DD HH:MM」
- 九段产出时点不同（V6 凌晨 02:45、要闻早间、政策 17:30 EOD、量化持仓按季、
  ①③ 为本次 collect），只写日期会让人误以为全是刚刷新。
- 页面头部「生成于」改为「页面渲染于」，避免与各段产出时间混淆。
- ② 要闻时间取 digest 源文件最后写入时刻（collect 只负责合并，不伪造为当前时间）。
"""

import glob
import html
import json
import os
import re
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
RISK_JSON = os.path.join(DASHBOARD_PATH, "risk_state.json")
# 出场纪律单一真源（与 09:20 盘前卡 / V6 日报共用，2026-11-27）
EXIT_RULES_PATH = os.path.expanduser("~/projects/trading-journal/config/exit_rules.json")
EXIT_RULES_DEFAULT = {"stop_warn_pct": 0.08, "stop_hard_pct": 0.13,
                      "trail_activate_pct": 0.10, "trail_dd_pct": 0.08}


def load_exit_rules() -> dict:
    try:
        with open(EXIT_RULES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: float(raw[k]) for k in EXIT_RULES_DEFAULT}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 出场纪律参数不可用 {EXIT_RULES_PATH} ({exc})，回退内置默认",
              file=sys.stderr)
        return dict(EXIT_RULES_DEFAULT)


EXIT = load_exit_rules()
EXIT_ACT = EXIT["trail_activate_pct"]
EXIT_DD = EXIT["trail_dd_pct"]
EXIT_WARN = EXIT["stop_warn_pct"]
EXIT_HARD = EXIT["stop_hard_pct"]
QUANT_ACTIVITY_JSON = os.path.join(DASHBOARD_PATH, "cache", "quant_activity.json")
QUANT_HOLDINGS_JSON = os.path.join(DASHBOARD_PATH, "cache", "quant_holdings.json")


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
.pbar { height:7px; border-radius:4px; background:#eef2f7; overflow:hidden; margin:3px 0 8px; }
.pbar > i { display:block; height:100%; background:#1a73e8; }
.prow { display:flex; align-items:baseline; gap:8px; font-size:12px; color:#607d8b; }
.prow .k { width:64px; flex:0 0 auto; }
.prow .v { margin-left:auto; font-variant-numeric:tabular-nums; color:#263238; font-weight:600; }
"""


def _fmt(v, suffix="", default="—"):
    if v is None or v == "":
        return f'<span class="dash">{default}</span>'
    return f"{v}{suffix}"


def _refresh_tag(ts, label="刷新"):
    """把各数据源异构时间戳统一成「· 刷新 MM-DD HH:MM」，挂在模块标题行。

    九段数据的产出时点本来就不同：V6 凌晨 02:45、要闻早间、政策 17:30 EOD、
    量化持仓按季、①③ 为本次 collect 时刻。把它们都标出来，避免读者把
    「凌晨的温度」误当成「刚刚刷新」。解析不了就不显示，不伪造时间。
    """
    if ts in (None, ""):
        return ""
    s = str(ts).strip().replace("T", " ")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", s)
    if m:
        txt = f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    elif re.fullmatch(r"\d{8}", s):
        txt = f"{s[4:6]}-{s[6:8]}（仅日期）"
    else:
        return ""
    return (f'<span style="font-size:11.5px;color:#90a4ae;font-weight:400;'
            f'margin-left:8px">· {label} {txt}</span>')


def _etf_data_note(etf):
    """① 段标题行的「数据到达」注记。

    2026-11-26 用户问「刷新为什么是 01:17 而不是次日 8/9 点」：01:17 是脚本
    「深市连续 2 次不变」的确认时刻，份额实际是前一晚 22:17(沪)/23:17(深) 到的。
    只显示 generated_at 会让人误以为 01:17 才抓数。这里把两侧数据到达时刻与
    次日复核结果显式标出来；没有该字段就不显示（旧数据不伪造）。
    """
    parts = []
    if etf.get("sh_published_at"):
        parts.append(f"沪 {etf['sh_published_at']}")
    if etf.get("sz_snapshot_at"):
        parts.append(f"深 {etf['sz_snapshot_at']}")
    if not parts:
        return ""
    rev = etf.get("sz_revised_at") or etf.get("sh_revised_at")
    extra = (f" · 次日 {rev} 复核修订" if rev
             else (" · 次日复核已修订" if etf.get("sz_morning_revision") else ""))
    return (f'<span style="font-size:11.5px;color:#90a4ae;font-weight:400;margin-left:8px">'
            f'· 数据到达 {" / ".join(parts)}{extra}</span>')


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

    def _total_cell(p):
        """合计列：单市场口径加 *，避免把「仅深市」误读成沪深合计。"""
        v = _fmt(p.get("net_yi"))
        return v + ("*" if (p.get("complete") is False and p.get("net_yi") is not None) else "")

    rows = "".join(
        f'<tr><td>{p.get("date","")}</td>'
        f'<td class="num">{_fmt(p.get("sh_yi"))}</td>'
        f'<td class="num">{_fmt(p.get("sz_yi"))}</td>'
        f'<td class="num">{_total_cell(p)}</td>'
        f'<td class="num">{_fmt(main_map.get(p.get("date")))}</td></tr>'
        for p in tail)

    sz_txt = f"{sz_today}亿" if sz_today is not None else "次日起追踪"
    sh_txt = f"{sh_today}亿" if sh_today is not None else "T+1 未发布"
    sz_state = "·已确认" if etf.get("sz_final") else ("·更新中" if sz_today is not None else "")
    incomplete_today = etf.get('netbuy_today_complete') is False
    _miss = '、'.join(etf.get('netbuy_today_missing') or []) or '单侧市场'
    _inc_line = (f'<div style="font-size:11.5px;color:#b26a00;margin-top:4px">'
                 f'⚠️ 今日仅单市场口径（缺{_miss}）：已标 incomplete，'
                 f'<b>不计分位、不触发信号</b></div>') if incomplete_today else ''
    return f"""<section class="section" id="sec-national">
<h2>① 国家队资金动向 <span style="font-size:12px;color:#90a4ae;font-weight:400">主指标＝宽基ETF份额净申购</span>{_refresh_tag(d.get('generated_at'))}{_etf_data_note(etf)}</h2>
<div class="sub">{etf.get('netbuy_basis','')}</div>
<div class="flow-grid">
  <div class="flow-box primary"><div style="font-size:12px;color:#607d8b;margin-bottom:4px">宽基ETF今日<b>净申购</b>（沪深合计·国家队代理）</div>
    <span class="big-num {net_color}">{_fmt(net_today, '亿')}</span><br>{_sparkline(net, 'net_yi')}
    <div style="font-size:11.5px;color:#607d8b;margin-top:4px;line-height:1.9">
      沪市（T+1） <b>{sh_txt}</b> · 深市（当日{sz_state}） <b>{sz_txt}</b><br>
      合计 <b>{_fmt(net_today)}亿</b>{'（暂缺沪市）' if incomplete_today else ''}</div>
    {_inc_line}
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
<div class="table-scroll"><table><thead><tr><th>日期</th><th class="num">沪市净申购(亿)</th><th class="num">深市净申购(亿)</th><th class="num">沪深合计(亿)</th><th class="num">主力净流入(亿)</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p style="font-size:11.5px;color:#90a4ae;margin-top:8px">
沪市＝上交所 ETF 份额（<b>T+1 发布</b>，官方口径约次日 23:00 更新，故当日 18:30 日更通常为空，次日晚间自动补抓并刷新本页）；
深市＝深交所当日快照（自 2026-11-09 起逐日累积）；18:30 后会逐小时复核，连续 2 次无变化才标「已确认」。当日任一市场缺失 → 标 incomplete，<b>不计分位、不触发信号</b>。<br>
<code>*</code> 该日仅单市场口径（另一侧未发布/未累积），<b>不是沪深合计</b>。<br>
北向单日净买额自 2024-08 起停止披露，已从本模块移除。</p>
</section>"""


# ---------------- ② 财经要闻 ----------------
def render_module2(d):
    news = d.get('news_digest', {}) or {}
    if not news.get('items') and not news.get('themes_points'):
        return ('<section class="section" id="sec-news"><h2>② 今日财经要闻</h2>'
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
    return (f'<section class="section" id="sec-news"><h2>② 今日财经要闻 '
            f'<span style="font-size:12px;color:#90a4ae;font-weight:400">{ds} · Sina News AI归纳 · {src}个主题</span>'
            f'{_refresh_tag(news.get("generated_at"))}</h2>'
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
    return (f'<section class="section" id="sec-signals"><h2>③ 投资信号 '
            f'<span style="font-size:12px;color:#90a4ae;font-weight:400">滚动分位触发 · '
            f'{sig.get("triggered_count",0)}/{total} 已触发</span>'
            f'{_refresh_tag(d.get("generated_at"))}</h2>'
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
    head = ('<section class="section" id="sec-v6"><h2>④ V6 交易决策 '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            'trend-trading-system · 波段决策单 · 100-500亿域</span>'
            + _refresh_tag(s.get("generated_at") if isinstance(s, dict) else None)
            + '</h2>')
    if not s:
        return head + f'<p style="color:#90a4ae;font-size:13px">{note or "无数据"}</p></section>'

    t = s.get("temperature") or {}
    pos = s.get("position") or {}
    risk = s.get("index_risk") or {}
    rg = s.get("risk_guard") or {}
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
        (f"{emo.get('phase') or '—'}"
         f"{(' · ' + emo['phase_detail']) if emo.get('phase_detail') else ''}"
         f" · 涨停 {emo.get('limit_up')} / 跌停 {emo.get('limit_down')}")
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
    <div class="kv" style="color:#7c3aed">🛡 已有浮盈仓（防守）：见 ⑦ 浮盈保护{('（' + str(rg.get('state_label')) + '(' + str(rg.get('score')) + '/8)' + '）') if rg.get('ok') else '（风险状态不可用）'}</div>
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


# ─────────────────── ⑤ 政策信号（跨系统 · policy-signal） ───────────────────

POLICY_FILE = os.path.join(DASHBOARD_PATH, "policy", "latest.json")
POLICY_PAGE = "https://stranger971020.github.io/hermes-dashboard/policy/"
POLICY_MAX_AGE_DAYS = 7   # 工作日 17:30 EOD；长假容忍到 7 天

BAND_MEANING = {
    "可能改变趋势": "力度足以改变趋势 —— 可按仓位上限执行",
    "托底/压尾部": "托底、压尾部风险为主 —— 趋势不改",
    "形式大于实质": "有动作但力度不足 —— 只托不举",
    "无效": "该市场位置政策推不动 —— 别把政策当催化",
}


def _position_zone(dd):
    """市场位置分档（与 policy-signal 页同一口径）。"""
    if not isinstance(dd, (int, float)):
        return "—"
    if dd <= -15:
        return "有效区（历史政策起效区）"
    if dd <= -13:
        return "擦边区（再跌 2% 才进有效区）"
    if dd <= -5:
        return "中等回调（历史上政策常无效）"
    return "高位区（政策推不动）"


def load_policy_summary():
    try:
        with open(POLICY_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None, ("政策摘要尚未生成 —— policy-signal 在工作日 17:30 EOD 产出 "
                      "policy/latest.json。")
    except Exception as e:  # noqa: BLE001
        return None, f"政策摘要解析失败: {e}"
    if d.get("schema") != 1:
        return None, f"政策摘要 schema 不匹配: {d.get('schema')}（期望 1）"
    return d, None


def render_module_policy():
    p, note = load_policy_summary()
    head = ('<section class="section" id="sec-policy"><h2>⑤ 政策信号 '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            'policy-signal · 政策力度 × 市场位置 → 仓位上限</span>'
            + _refresh_tag(p.get("generated_at") if isinstance(p, dict) else None)
            + '</h2>')
    if not p:
        return head + f'<p style="color:#90a4ae;font-size:13px">{note or "无数据"}</p></section>'

    bd = p.get("breakdown") or {}
    mk = p.get("market") or {}
    ex = p.get("expect") or {}
    cnt = p.get("counts") or {}
    hits = p.get("hits") or []
    recent = p.get("recent") or []

    band = p.get("band") or "—"
    score = p.get("score")
    cap = p.get("cap")
    band_cls = {"可能改变趋势": "down", "托底/压尾部": "", "形式大于实质": "flat", "无效": "up"}.get(band, "")

    quality = ""
    if p.get("degraded"):
        gap = p.get("coverage_gap")
        quality = (f'<div class="warn-note">⚠️ 本日数据降级：{p.get("coverage") or "—"} —— '
                   f'评分基于部分数据源，读数和仓位上限打折看。'
                   + (f'<br>采集缺口：{gap}（窗口未覆盖，本页的“无动作”不可作结论）' if gap else "")
                   + '</div>')
    org = p.get("origins") or {}
    if org.get("n"):
        _names = {"pbc": "央行", "csrc": "证监会", "nfra": "金融监管总局", "huijin": "中央汇金",
                  "mof": "财政部", "ndrc": "发改委", "sasac": "国资委", "safe": "外汇局",
                  "gov": "国务院", "relay": "转载", "exchange": "交易所",
                  "sse_ex": "上交所", "szse_ex": "深交所", "bse_ex": "北交所"}
        _top = _names.get(str(org.get("top")), org.get("top"))
        quality += (f'<div style="font-size:12px;color:#90a4ae;margin:6px 0 0">'
                    f'信源 {org["n"]} 个 / 事实 {org.get("facts") or 0} 条 · '
                    f'最大 {float(org.get("top_share") or 0):.0%}（{_top}）· '
                    f'熵 {float(org.get("entropy") or 0):.2f} · '
                    f'纯转载 {float(org.get("relay_share") or 0):.0%}</div>')
    if p.get("calendar_missing"):
        quality += ('<div class="warn-note">⚠️ 有预期事件到期未捕获（LPR/MLF/会议等）：'
                    '本日“无动作”需复核</div>')

    kpi = ('<div class="flow-grid">'
           + _v6_box("🧮 政策力度分", f'{score}<span style="font-size:13px;color:#90a4ae">/100</span>'
                     if isinstance(score, (int, float)) else "—",
                     f"档位：{band}", band_cls)
           + _v6_box("📏 仓位上限（政策给出的 cap）",
                     f"{cap}%" if isinstance(cap, (int, float)) else "—",
                     BAND_MEANING.get(band, ""))
           + _v6_box("📍 市场位置（决定政策有没有用）",
                     f"{mk.get('dd250')}%" if mk.get("dd250") is not None else "—",
                     _position_zone(mk.get("dd250")))
           + '</div>')

    # ── 力度构成 ──
    dims = [("金额", bd.get("amount"), 15), ("风险处置", bd.get("risk"), 15),
            ("机构资金", bd.get("inst"), 15), ("制度支持", bd.get("support"), 15),
            ("规模分", bd.get("scale"), 6)]
    bars = ""
    for name, v, mx in dims:
        w = 0 if not isinstance(v, (int, float)) else max(0, min(100, v / mx * 100))
        bars += (f'<div class="prow"><span class="k">{name}</span>'
                 f'<span class="v">{v if v is not None else "—"}</span></div>'
                 f'<div class="pbar"><i style="width:{w:.0f}%"></i></div>')
    formula = (f'力度 {p.get("effect_score")} × 位置系数 {p.get("position_coef")} '
               f'− 杠杆罚分 {p.get("leverage_penalty")} = <b>{score}</b>'
               + ('　·　否决触发（高杠杆 + 20日弱势 → 直接归零）' if p.get("veto") else '')
               + ('　·　监管收紧在案 → cap 减半' if p.get("cooling") else ''))
    build_html = ('<div class="flow-grid" style="margin-top:14px"><div class="flow-box primary">'
                  '<div style="font-size:12px;color:#607d8b;margin-bottom:6px">'
                  f'力度构成（条长 = 占该项满分比例 · 合计 {p.get("effect_score")}/60）</div>'
                  + bars
                  + f'<div class="kv" style="margin-top:6px">{formula}</div>'
                  + f'<div class="kv">同档历史（未来 60 日）：区间 <b>{ex.get("r60_low")}% ~ {ex.get("r60_high")}%</b>'
                    f' · 最大回撤 <b>{ex.get("max_dd")}%</b></div>'
                  + '</div></div>')

    # ── 今日动作 ──
    if hits:
        items = "".join(
            (f'<div style="padding:7px 0;border-bottom:1px solid #f5f5f5;font-size:13px">'
             f'<span class="tname">{h.get("dept") or "—"}</span>'
             f'<span style="color:#90a4ae;font-size:12px">{h.get("type") or "—"}</span>　'
             + (f'<a class="news-item" style="display:inline" href="{h.get("url")}" target="_blank" rel="noopener">{h.get("title")}</a>'
                if h.get("url") else f'<span>{h.get("title")}</span>')
             + '</div>')
            for h in hits[:5])
        act_html = (f'<div style="margin-top:14px;font-size:12px;color:#607d8b">'
                    f'今日动作（去重后 {len(hits)} 条，按信息量排序取前 5）</div>' + items)
    else:
        act_html = ('<div class="kv" style="margin-top:14px">今日动作：'
                    '本期无偏多政策事件（扫描 '
                    f'{cnt.get("scanned") or "—"} 条 / 命中 {cnt.get("hit") or 0} 条）</div>')

    # ── 过去 7 天政策 ──
    ra = p.get("recent_actions_7d") or []
    week_html = ""
    if ra:
        rows = "".join(
            (f'<div style="padding:5px 0;border-bottom:1px solid #f5f5f5;font-size:12px">'
             f'<span style="color:#90a4ae">{html.escape(str(x.get("date") or ""))}</span> · '
             f'<span class="tname">{html.escape(str(x.get("dept") or "—"))}</span> · '
             f'<span style="color:#90a4ae">{html.escape(str(x.get("type") or "—"))}</span>　'
             + (f'<a class="news-item" style="display:inline" href="{html.escape(str(x.get("url")))}" '
                f'target="_blank" rel="noopener">{html.escape(str(x.get("title") or ""))}</a>'
                if x.get("url") else html.escape(str(x.get("title") or "")))
             + '</div>')
            for x in ra[:12])
        week_html = ('<div style="margin-top:14px;font-size:12px;color:#607d8b">'
                     f'过去 7 天政策（最近 {min(len(ra), 12)} 条，自动抽取）</div>' + rows)

    # ── 最近读数 ──
    def _mmdd(r):
        v = str(r.get("trade_date_iso") or r.get("trade_date") or "")
        if len(v) == 8 and v.isdigit():
            return f"{v[4:6]}-{v[6:]}"
        return v[5:] if len(v) >= 10 else v

    series = [{"date": _mmdd(r), "score": r.get("score")} for r in reversed(recent)]
    spark = _sparkline(series, "score", width=560, height=52) if series else ""
    recent_html = (f'<div style="margin-top:14px;font-size:12px;color:#607d8b">最近读数（日）'
                   f'{spark}'
                   f'<div style="font-size:11px;color:#90a4ae">'
                   f'{recent[-1].get("trade_date_iso") if recent else ""} → '
                   f'{recent[0].get("trade_date_iso") if recent else ""}'
                   f' · 最新 {score} / 档位 {band}</div></div>')

    meta = (f'<div style="margin-top:10px;font-size:12px;color:#607d8b;line-height:1.8">'
            f'数据日期 <b>{p.get("trade_date_iso") or p.get("trade_date")}</b>'
            f' · 生成 {p.get("generated_at")}'
            f' · 规则版本 {p.get("rules_ver") or "—"}'
            f' · 覆盖 {p.get("coverage") or "—"}<br>'
            f'<a class="news-item" style="display:inline" href="{p.get("links", {}).get("page") or POLICY_PAGE}" '
            f'target="_blank" rel="noopener">→ 政策信号完整页（① 今日动作 ② 力度构成 ③ 市场位置 ④ 风险 ⑤ 所以怎么做）</a></div>')

    return head + quality + kpi + build_html + act_html + week_html + recent_html + meta + '</section>'


def render_module_emotion():
    """⑥ 情绪温度计 —— 市场状态提示（不是卖出信号）。

    定位来自 2026-11-21 的定标实测（backtest/emotion_calib.py，2729 个交易日）：
      · 涨占比 < 0.25 之后，未来 5 日沪深300 均值 +0.34%、下跌概率 42%（全样本基准 48%）
      · 涨占比 < 0.25 且涨停数 < 50% × 20日均值 → 未来 5 日 +0.97%、下跌概率 37%
      · 强势股（近20日涨幅前20%）在退潮日的未来 5 日 -0.24%，与弱势组 -0.08% 几无差异
    ⇒ 「情绪退潮 → 卖出」是负期望。这一段的用处是：
      ① 退潮期不要追高 / 不要加仓（波动放大，尾部风险变厚）
      ② 顺手检查持仓集中度（真正放大回撤的是集中度，不是没兑现）
      ③ 预期未来几天振幅变大，别把正常波动当趋势破坏
    """
    s, note = load_v6_summary()
    head = ('<section class="section" id="sec-emotion"><h2>⑥ 情绪温度计 '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            '退潮/冰点判读 · <b>状态提示，非卖出信号</b></span>'
            + _refresh_tag(s.get("generated_at") if isinstance(s, dict) else None)
            + '</h2>')
    if not s:
        return head + f'<p style="color:#90a4ae;font-size:13px">{note or "无数据"}</p></section>'
    emo = s.get("emotion") or {}
    if not emo:
        return head + '<p style="color:#90a4ae;font-size:13px">V6 摘要未包含情绪模块</p></section>'

    ur = emo.get("updown_ratio")
    lu, ld = emo.get("limit_up"), emo.get("limit_down")
    score, phase = emo.get("emo_score"), emo.get("phase") or "—"
    phase_detail = emo.get("phase_detail") or ""
    if isinstance(ur, (int, float)):
        if ur < 0.25:
            state, cls = ("冰点/退潮 — 禁止追高与加仓", "up")
        elif ur < 0.5:
            state, cls = ("偏弱 — 谨慎加仓", "")
        else:
            state, cls = ("正常", "down")
    else:
        state, cls = ("—", "")

    box1 = _v6_box("📉 涨跌家数比（涨/跌）", _v6_pct(ur), state, cls)
    box2 = _v6_box("🔴 涨停 / 跌停",
                   f"{lu if lu is not None else '—'}<span style='font-size:13px;color:#90a4ae'>/</span>{ld if ld is not None else '—'}",
                   f"晋级率 {_v6_pct(emo.get('promotion_rate'))} · 最高连板 {emo.get('max_streak') if emo.get('max_streak') is not None else '—'}")
    box3 = _v6_box("🌡️ 情绪分", (f"{score}" if score is not None else "—") + "<span style='font-size:13px;color:#90a4ae'>/100</span>",
                   f"{phase}{(' · ' + phase_detail) if phase_detail else ''} · 恐慌分 {emo.get('panic_score') if emo.get('panic_score') is not None else '—'}")

    # 近 5 日趋势
    hist = emo.get("hist5") or []
    rows = "".join(
        f"<tr><td>{h.get('date')}</td><td>{_v6_pct(h.get('updown_ratio'))}</td>"
        f"<td>{h.get('limit_up')}</td><td>{h.get('max_streak')}</td>"
        f"<td>{h.get('promotion_rate')}</td><td>{h.get('emo_score')}</td></tr>"
        for h in hist)
    trend = (f'<div class="table-scroll" style="margin-top:12px"><table><thead><tr>'
             f'<th>日期</th><th>涨跌家数比</th><th>涨停</th><th>最高连板</th><th>晋级率</th><th>情绪分</th>'
             f'</tr></thead><tbody>{rows}</tbody></table></div>') if rows else ""
    advice = emo.get("advice")
    advice_html = f"<br><b>系统原文：</b>{advice}" if advice else ""

    return head + f"""
<div class="flow-grid" style="margin-top:12px">{box1}{box2}{box3}</div>
{trend}
<div class="kv" style="margin-top:12px">判读规则：涨跌家数比 <b>&lt;0.50</b> 偏弱 · <b>&lt;0.25</b> 退潮/冰点（禁止追高与加仓）· 跌停 ≥25 家 → 冰点档（历史上 T+1 胜率反而抬升）· 情绪分 <b>≥65</b> 为高潮/过热（<b>反向</b>：历史未来 5 日偏弱）；当日分 - 5 日均值 <b>≥25</b> 标「单日爆发」，否则「连续高潮」</div>
<p style="font-size:12px;color:#90a4ae;margin-top:8px">
<b>为什么这段不输出「卖出」：</b>2729 个交易日的实测里，涨占比 &lt;0.25 之后未来 5 日沪深300 平均
<b>+0.34%</b>（下跌概率 42%，低于基准 48%）；叠加「涨停骤降」后是 <b>+0.97%</b>（下跌概率 37%）。
强势股在退潮日的未来 5 日也仅 -0.24%，与弱势股（-0.08%）几乎无差异 —— 恐慌时减仓是负期望。
这一段的真实价值在<b>纪律</b>：退潮期不加仓、不追高、顺手看一眼集中度。
{advice_html}</p>
</section>"""


def load_risk_summary():
    """⑦ 段数据源：优先 V6 摘要的 risk_guard，回退本地 risk_state.json。

    单一事实源在 trend-trading-system/analysis/risk_state.py；V6 日报 02:45 生成并
    写入 summary["risk_guard"]，看板日更（工作日 18:30）只消费，不重算、不产生第二个判断。
    """
    s, _ = load_v6_summary()
    if isinstance(s, dict) and isinstance(s.get("risk_guard"), dict):
        p = dict(s["risk_guard"])
        p["_source"] = "v6_summary"
        p["_v6_trading_date"] = s.get("trading_date")
        p["_generated_at"] = s.get("generated_at")   # ⑦ 标题行的刷新时间取 V6 产出时刻
        return p, ""
    if os.path.exists(RISK_JSON):
        try:
            with open(RISK_JSON) as f:
                p = json.load(f)
            p["_source"] = "file_fallback"
            p["_generated_at"] = p.get("generated_at")
            return p, ""
        except Exception as e:  # noqa: BLE001
            return None, f"risk_state.json 解析失败: {e}"
    return None, "V6 摘要无 risk_guard，且 risk_state.json 尚未生成"


def render_module_risk():
    """⑦ 浮盈保护 · 市场脆弱度 + 动作框架。

    规则来自 2021-01~2026-10 的 100-500亿 PIT 事件研究（个股层，非组合代理）：
      · 脆弱/崩跌 + 近20日涨幅>=15%：20日 CVaR5 -39.5% → -8.3%，P(跌>10%) 43.0% → 0.6%
      · 弱市第二次冲击（10日内>=2次 80%下跌）：均值 -6.7% → -1.0%
      · 峰值浮盈 +12~18% 切片：最终浮亏概率 26% → 0%，回吐>10pp 42% → 3%
    动作：状态脆弱→初减25%，崩跌→40%，弱市第二次冲击→至少50%；
          剩余仓位以「持有期最高收盘价」为基准，浮盈曾达 +10% 后回撤 8% 收盘触发清仓剩余。
    """
    p, note = load_risk_summary()
    head = ('<section class="section" id="sec-risk"><h2>⑦ 浮盈保护 · 风险状态 '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            f'PIT 大盘脆弱度 + 持仓动作框架 · <b>初减 + 峰值回撤{EXIT_DD:.0%}清仓</b></span>'
            + _refresh_tag(p.get("_generated_at") if isinstance(p, dict) else None)
            + '</h2>')
    if not p:
        return (head + f'<p style="color:#90a4ae;font-size:13px">{note}。'
                f'运行 <code>python3 ~/projects/trend-trading-system/analysis/risk_state.py</code> 生成。</p></section>')

    m = p.get("metrics") or {}
    a = p.get("action") or {}
    comp = p.get("components") or {}
    state = p.get("state", "normal")
    label = p.get("state_label", state)
    score = p.get("score", 0)
    trim = a.get("initial_trim", 0) or 0
    ok = bool(p.get("ok", True))
    fresh = p.get("freshness") or {}
    stale = p.get("stale_days", fresh.get("stale_days", 0)) or 0
    source = p.get("_source", "")
    existing = p.get("existing") or {}
    new_entry = p.get("new_entry") or {}
    cls = "up" if state in ("fragile", "crash") else ("down" if state == "normal" else "")

    src_txt = ("来源 V6 日报" if source == "v6_summary"
               else "来源 risk_state.json（V6 摘要未含守卫，独立文件兜底）" if source == "file_fallback"
               else "")
    stale_txt = f" · 滞后 {stale} 个交易日" if stale else ""
    box1 = _v6_box("🧭 市场状态", label,
                   f"脆弱度 {score}/8 · asof {p.get('asof', '—')}{stale_txt}", cls)
    box2 = _v6_box("✂️ 建议初减", ("暂停" if not ok else f"{trim:.0%}"),
                   ("风险状态不可用 — 新仓建议暂停" if not ok
                    else f"剩余仓位峰值回撤{EXIT_DD:.0%}清仓"), "up" if (ok and trim >= 0.4) else "")
    box3 = _v6_box("📉 市场广度", _v6_pct(m.get("down_ratio")),
                   f"MA20上方 {_v6_pct(m.get('above_ma20'))} · 60日 {_v6_pct(m.get('g60'))}")

    warn_html = ""
    if not ok or stale:
        msg = "；".join(p.get("warnings") or ["风险状态未计算/数据滞后 — 新建仓建议暂停"])
        warn_html = (f"<div class='kv' style='margin-top:10px;color:#b45309;background:#fffbeb;"
                     f"padding:8px 10px;border-left:3px solid #f59e0b'>"
                     f"⚠️ {msg}。已有仓位按「浮盈曾达+{EXIT_ACT:.0%} 后峰值回撤"
                     f"{EXIT_DD:.0%}清仓剩余」纪律执行。</div>")
    if source == "file_fallback":
        warn_html += (f"<div class='kv' style='margin-top:6px;color:#90a4ae'>"
                      f"数据源：risk_state.json（V6 摘要未包含 risk_guard，使用独立文件兜底）。</div>")

    existing_text = existing.get("text") or a.get("trim_text") or (
        f"初减 {trim:.0%}；剩余仓位自峰值回撤{EXIT_DD:.0%}清仓"
        f"（需浮盈曾达+{EXIT_ACT:.0%}）" if ok else "风险状态不可用")
    ceiling_text = new_entry.get("ceiling_text") or "按 V6 温度档执行（无 V6 上下文对照）"
    divergence = new_entry.get("divergence")
    div_html = f"<br><span style='color:#b45309'>⚠️ {divergence}</span>" if divergence else ""
    division_html = (
        "<div class='kv' style='margin-top:12px'><b>与 V6 分工：</b>"
        f"④ = 进攻/新仓（温度档、行业、候选）；⑦ = 防守/已有浮盈（初减 + "
        f"峰值回撤{EXIT_DD:.0%}清仓）。"
        "两层并行、不互相覆盖。新仓/总仓上限见下；⑦ 的动作只作用于已有浮盈仓。</div>")

    volr = m.get("ivol_ratio")
    volr_s = f"{volr:.2f}×" if isinstance(volr, (int, float)) else "—"
    labels = [
        ("trend_60d_lt_-5", "沪深300 60日 &lt; -5%", _v6_pct(m.get("g60"))),
        ("trend_60d_lt_-10", "沪深300 60日 &lt; -10%", _v6_pct(m.get("g60"))),
        ("breadth_ma20_lt_30", "MA20 上方占比 &lt; 30%", _v6_pct(m.get("above_ma20"))),
        ("breadth_ma20_lt_20", "MA20 上方占比 &lt; 20%", _v6_pct(m.get("above_ma20"))),
        ("shock_today", "当日下跌占比 ≥ 80%", _v6_pct(m.get("down_ratio"))),
        ("second_shock_10d", "10 日内第二次冲击", f"{m.get('shock10', '—')} 次"),
        ("strong_basket_rolling", "强势篮子 5 日相对转负",
         _v6_pct(m.get("strong_ex5")) if m.get("strong_ex5") is not None else "—"),
        ("vol_expansion", "指数波动扩张 &gt; 1.3×", volr_s),
    ]
    rows = "".join(
        f"<tr><td>{lab}</td><td>{'✅' if comp.get(k) else '·'}</td>"
        f"<td class='num'>{val}</td></tr>" for k, lab, val in labels)

    hist = (p.get("history") or [])[-10:]
    slab = {"normal": "正常", "warning": "警戒", "fragile": "脆弱", "crash": "崩跌"}
    hrows = "".join(
        f"<tr><td>{h.get('trade_date')}</td><td>{slab.get(h.get('frag_state'), h.get('frag_state'))}</td>"
        f"<td class='num'>{int(h.get('frag_score', 0))}</td>"
        f"<td class='num'>{_v6_pct(h.get('down_ratio'))}</td>"
        f"<td class='num'>{_v6_pct(h.get('above_ma20'))}</td>"
        f"<td class='num'>{int(h.get('shock10', 0))}</td></tr>" for h in reversed(hist))

    v = p.get("validation") or {}
    fc = v.get("fragile_crash_15") or {}
    ss = v.get("second_shock_15") or {}
    pk = v.get("peak_12_18") or {}
    fh, fr = fc.get("hold") or {}, fc.get("rule") or {}
    sh, sr = ss.get("hold") or {}, ss.get("rule") or {}
    val_html = ""
    if fh and fr:
        val_html = (
            f"<div class='kv' style='margin-top:12px'><b>回测锚点</b>（{v.get('sample', {}).get('start', '—')}"
            f"~{v.get('sample', {}).get('end', '—')} · 100-500亿 PIT · 近20日涨幅≥15% · 未来20日）：<br>"
            f"脆弱/崩跌状态 20日 CVaR5：<b>{fh.get('cvar5', 0) * 100:.1f}% → {fr.get('cvar5', 0) * 100:.1f}%</b>"
            f" · P(跌&gt;10%)：{fh.get('p_lt_10', 0) * 100:.1f}% → {fr.get('p_lt_10', 0) * 100:.1f}%<br>"
            f"弱市第二次冲击：均值 <b>{sh.get('mean', 0) * 100:.1f}% → {sr.get('mean', 0) * 100:.1f}%</b>"
            f" · CVaR5 {sh.get('cvar5', 0) * 100:.1f}% → {sr.get('cvar5', 0) * 100:.1f}%<br>"
            f"峰值浮盈 +12~18% 切片：最终浮亏概率 <b>{pk.get('hold_p_final_neg', 0) * 100:.0f}% → "
            f"{pk.get('rule_p_final_neg', 0) * 100:.0f}%</b> · 回吐&gt;10pp "
            f"{pk.get('hold_p_giveback_gt10', 0) * 100:.0f}% → {pk.get('rule_p_giveback_gt10', 0) * 100:.0f}%<br>"
            f"<span style='color:#90a4ae'>口径：单边成本 0.15%；2022 年市值快照缺失用 2021-12-29 隐含股本前推；"
            f"不构成投资建议。</span></div>")

    return head + f"""
<div class="flow-grid" style="margin-top:12px">{box1}{box2}{box3}</div>
{warn_html}
<div class="table-scroll" style="margin-top:8px"><table><thead><tr>
<th>脆弱度构成</th><th>命中</th><th class="num">当前值</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="kv" style="margin-top:12px"><b>当前动作：</b>已有浮盈仓 {existing_text}。
新仓/总仓上限：<b>{ceiling_text}</b>。{div_html}</div>
{division_html}
<div class="flow-box" style="margin-top:12px">
  <div style="font-size:12px;color:#607d8b;margin-bottom:6px">🧮 移动止盈计算器（激活需峰值浮盈曾达+{EXIT_ACT:.0%}；峰值=持有期最高收盘价，不是成本价）</div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px">
    <label>成本价 <input id="rk-cost" type="number" step="0.01" style="width:110px;padding:4px"></label>
    <label>峰值价 <input id="rk-peak" type="number" step="0.01" style="width:110px;padding:4px"></label>
    <label>当前价 <input id="rk-cur" type="number" step="0.01" style="width:110px;padding:4px"></label>
    <button onclick="rkCalc()" style="padding:4px 12px;cursor:pointer">计算</button>
    <span id="rk-out" style="color:#607d8b">输入成本价、峰值价与当前价</span>
  </div>
</div>
<div class="table-scroll" style="margin-top:12px"><table><thead><tr>
<th>日期</th><th>状态</th><th class="num">分数</th><th class="num">下跌占比</th>
<th class="num">MA20上方</th><th class="num">10日冲击</th></tr></thead><tbody>{hrows}</tbody></table></div>
{val_html}
<script>
function rkCalc() {{
  var k = parseFloat(document.getElementById('rk-cost').value);
  var p = parseFloat(document.getElementById('rk-peak').value);
  var c = parseFloat(document.getElementById('rk-cur').value);
  var out = document.getElementById('rk-out');
  if (!isFinite(p) || !isFinite(c) || p <= 0 || c <= 0) {{ out.textContent = '输入峰值价与当前价'; return; }}
  var dd = c / p - 1;
  var pct = function (x) {{ return (x * 100).toFixed(1) + '%'; }};
  if (!isFinite(k) || k <= 0) {{
    out.innerHTML = '当前回撤 <b>' + pct(dd) + '</b>（未填成本价，无法判断是否已激活）';
    return;
  }}
  var peakGain = p / k - 1;
  if (peakGain < {EXIT_ACT}) {{
    out.innerHTML = '峰值浮盈仅 <b>' + pct(peakGain) + '</b> · <b style="color:#607d8b">移动止盈未激活</b>（需曾达 +{EXIT_ACT:.0%}）';
  }} else if (dd <= -{EXIT_DD}) {{
    out.innerHTML = '当前回撤 <b>' + pct(dd) + '</b> → <b style="color:#c62828">已触发 {EXIT_DD:.0%} 峰值回撤，清仓剩余</b>';
  }} else {{
    out.innerHTML = '已激活 · 当前回撤 <b>' + pct(dd) + '</b> · 距 {EXIT_DD:.0%} 回撤线还有 <b>' +
      ((dd + {EXIT_DD}) * 100).toFixed(1) + 'pp</b>';
  }}
}}
</script>
</section>"""


def load_quant_activity():
    if not os.path.exists(QUANT_ACTIVITY_JSON):
        return None, "cache/quant_activity.json 尚未生成"
    try:
        with open(QUANT_ACTIVITY_JSON) as f:
            return json.load(f), ""
    except Exception as e:  # noqa: BLE001
        return None, f"quant_activity.json 解析失败: {e}"


def render_module_quant_activity():
    """⑧ 量化参与度（代理）——不是成交占比/持仓占比。

    代理公式（与 backtest/quant_activity_validate.py 同口径）：
      mean(z(换手率), z(振幅), z(|日内涨跌|), z(小单占比))，每日横截面、成交额加权。
    验证：2024-01 微盘崩 / 2024-02 量化平仓，事件前 10 日 micro-large spread
    相对前 60 日 z≈+1.0，事件后回落；安慰剂日期未通过。
    """
    p, note = load_quant_activity()
    head = ('<section class="section" id="sec-quant"><h2>⑧ 量化参与度（代理） '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            '活跃度代理 · <b>不是成交占比/持仓占比</b></span>'
            + _refresh_tag(p.get("generated_at") if isinstance(p, dict) else None)
            + '</h2>')
    if not p:
        return (head + f'<p style="color:#90a4ae;font-size:13px">{note}。'
                f'运行 <code>python3 ~/projects/hermes-dashboard/collect_quant_activity.py</code> 生成。</p></section>')

    m = p.get("market") or {}
    cov = p.get("coverage") or {}
    hist = p.get("history") or []
    caveat = (p.get("proxy") or {}).get("caveat") or "代理，不是量化成交占比。"
    formula = (p.get("proxy") or {}).get("formula") or ""

    box1 = _v6_box("📊 市场活跃度", f"{m.get('score', '—')}<span style='font-size:13px;color:#90a4ae'>/100</span>",
                   f"z {m.get('z', 0):+.2f} · 60日分位")
    box2 = _v6_box("🔬 微盘活跃度", f"{m.get('micro_score', '—')}<span style='font-size:13px;color:#90a4ae'>/100</span>",
                   f"vs 大盘 spread {m.get('spread_score', '—')}/100")
    box3 = _v6_box("🏛 大盘活跃度", f"{m.get('large_score', '—')}<span style='font-size:13px;color:#90a4ae'>/100</span>",
                   f"micro-large z {m.get('spread_z', 0):+.2f}")

    def _ind_rows(items):
        return "".join(
            f"<tr><td>{x.get('name', '—')}</td><td class='num'>{x.get('score_pct', 0):.0f}</td>"
            f"<td class='num'>{x.get('n', 0)}</td><td class='num'>{x.get('turnover', 0):.1f}%</td></tr>"
            for x in (items or []))

    stock_rows = "".join(
        f"<tr><td>{x.get('name', '—')}</td><td style='color:#90a4ae'>{x.get('ts_code', '—')}</td>"
        f"<td>{x.get('industry', '—')}</td><td class='num'>{x.get('score', 0):.1f}</td>"
        f"<td class='num'>{x.get('turnover_rate', 0):.1f}%</td>"
        f"<td class='num'>{x.get('small_share', 0) * 100:.0f}%</td>"
        f"<td class='num {('up' if x.get('intraday', 0) < 0 else 'down')}'>{x.get('intraday', 0):+.1f}%</td>"
        f"<td class='num'>{x.get('range', 0):.1f}%</td></tr>"
        for x in (p.get("top_stocks") or [])[:10])

    h20 = hist[-20:]
    spark = (
        f"<div class='kv' style='margin-top:12px'><b>近20日：</b>"
        f"<div><span style='color:#1a73e8'>市场</span>{_sparkline(h20, 'market', width=560, height=44)}</div>"
        f"<div><span style='color:#7c3aed'>微盘</span>{_sparkline(h20, 'micro', width=560, height=44)}</div>"
        f"<div><span style='color:#c62828'>spread</span>{_sparkline(h20, 'spread', width=560, height=44)}</div></div>")

    return head + f"""
<div class="kv" style="margin-top:10px;background:#fff7ed;border-left:3px solid #f59e0b;padding:8px 10px;color:#92400e">
  ⚠️ {caveat}<br>
  <span style="font-size:11px;color:#a16207">公式：{formula} · 覆盖 {cov.get('n_stocks', '—')} 只 / {cov.get('n_industries', '—')} 行业 · asof {p.get('asof', '—')}</span>
</div>
<div class="flow-grid" style="margin-top:12px">{box1}{box2}{box3}</div>
{spark}
<div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:14px">
  <div style="flex:1;min-width:320px">
    <div class="kv"><b>活跃度最高行业</b></div>
    <div class="table-scroll"><table><thead><tr><th>行业</th><th class="num">分位</th><th class="num">成分股</th><th class="num">换手</th></tr></thead>
    <tbody>{_ind_rows(p.get('top_industries')) or '<tr><td colspan="4">—</td></tr>'}</tbody></table></div>
  </div>
  <div style="flex:1;min-width:320px">
    <div class="kv"><b>活跃度最低行业</b></div>
    <div class="table-scroll"><table><thead><tr><th>行业</th><th class="num">分位</th><th class="num">成分股</th><th class="num">换手</th></tr></thead>
    <tbody>{_ind_rows(p.get('bottom_industries')) or '<tr><td colspan="4">—</td></tr>'}</tbody></table></div>
  </div>
</div>
<div class="kv" style="margin-top:14px"><b>个股活跃度 Top10</b>
  <span style="color:#90a4ae">（换手/振幅/日内/小单占比合成；高分=更像高频/量化活跃，不等于量化持仓）</span></div>
<div class="table-scroll"><table><thead><tr>
<th>名称</th><th>代码</th><th>行业</th><th class="num">活跃度</th><th class="num">换手</th>
<th class="num">小单占比</th><th class="num">日内</th><th class="num">振幅</th></tr></thead>
<tbody>{stock_rows or '<tr><td colspan="8">—</td></tr>'}</tbody></table></div>
<div class="kv" style="margin-top:10px;color:#90a4ae">
  验证：2024-01 微盘崩 / 2024-02 量化平仓，事件前 10 日 micro-large spread 相对前 60 日
  <b>z≈+1.0</b>，事件后回落；安慰剂日期未通过。代理只用于拥挤度/活跃度排序，<b>不能回答「量化基金占多少」</b>。
</div>
</section>"""


def load_quant_holdings():
    if not os.path.exists(QUANT_HOLDINGS_JSON):
        return None, "cache/quant_holdings.json 尚未生成"
    try:
        with open(QUANT_HOLDINGS_JSON) as f:
            return json.load(f), ""
    except Exception as e:  # noqa: BLE001
        return None, f"quant_holdings.json 解析失败: {e}"


def render_module_quant_holdings():
    """⑨ 量化持仓（公募·季度·下限）——不是全部量化持仓。"""
    p, note = load_quant_holdings()
    head = ('<section class="section" id="sec-holdings"><h2>⑨ 量化持仓（季度·公募下限） '
            '<span style="font-size:12px;color:#90a4ae;font-weight:400">'
            '仅公募·前十大·不含私募 · <b>下限而非全量</b></span>'
            + _refresh_tag(p.get("generated_at") if isinstance(p, dict) else None)
            + '</h2>')
    if not p:
        return (head + f'<p style="color:#90a4ae;font-size:13px">{note}。'
                f'运行 <code>python3 ~/projects/hermes-dashboard/collect_quant_holdings.py --force</code> 生成。</p></section>')

    cov = p.get("coverage") or {}
    m = p.get("market") or {}
    ok = int(cov.get("n_funds_ok") or 0)
    base = int(cov.get("n_funds_base") or 0)
    ratio = ok / base if base else 0
    complete = ratio >= 0.8
    caveat = m.get("caveat") or "仅公募·前十大·不含私募；结果是下限。"

    warn = ""
    if not complete:
        warn = (f"<div class='kv' style='margin-top:10px;background:#fef2f2;border-left:3px solid #dc2626;"
                f"padding:8px 10px;color:#991b1b'>⚠️ 基金抓取覆盖不足：{ok}/{base}（{ratio*100:.0f}%）。"
                f"本期占比数字不可用，只展示已抓到的样本；请稍后重跑。</div>")

    if complete:
        kpi = f"""
<div class="flow-grid" style="margin-top:12px">
  {_v6_box("📅 季度", p.get("quarter", "—"), f"截至 {p.get('quarter_end', '—')}")}
  {_v6_box("💰 公募量化前十大持仓", f"{m.get('total_holding_yi', 0):.1f}<span style='font-size:13px;color:#90a4ae'>亿</span>",
           f"基金 {ok}/{base} · 股票 {cov.get('n_stocks', '—')}")}
  {_v6_box("📊 占 A 股流通市值", f"{m.get('share_pct', 0):.3f}<span style='font-size:13px;color:#90a4ae'>%</span>",
           f"分母 {m.get('total_circ_yi', 0):.0f}亿 · 下限")}
</div>"""
    else:
        kpi = f"""
<div class="flow-grid" style="margin-top:12px">
  {_v6_box("📅 季度", p.get("quarter", "—"), f"截至 {p.get('quarter_end', '—')}")}
  {_v6_box("💰 已抓到持仓", f"{m.get('total_holding_yi', 0):.1f}<span style='font-size:13px;color:#90a4ae'>亿</span>",
           f"基金 {ok}/{base} · 覆盖不足")}
  {_v6_box("📊 占流通市值", "—", "覆盖不足，暂不展示")}
</div>"""

    def _ind_rows(items):
        return "".join(
            f"<tr><td>{x.get('name', '—')}</td><td class='num'>{x.get('holding_yi', 0):.2f}</td>"
            f"<td class='num'>{x.get('n', 0)}</td>"
            f"<td class='num'>{(x.get('own_pct') if x.get('own_pct') is not None else 0):.3f}%</td></tr>"
            for x in (items or [])[:8])

    def _stock_rows(items):
        return "".join(
            f"<tr><td>{x.get('name', '—')}</td><td style='color:#90a4ae'>{x.get('ts_code', '—')}</td>"
            f"<td>{x.get('industry', '—')}</td><td class='num'>{x.get('holding_yi', 0):.2f}</td>"
            f"<td class='num'>{x.get('n_funds', 0)}</td>"
            f"<td class='num'>{(x.get('own_pct') if x.get('own_pct') is not None else 0):.3f}%</td></tr>"
            for x in (items or [])[:10])

    return head + f"""
<div class="kv" style="margin-top:10px;background:#eff6ff;border-left:3px solid #3b82f6;padding:8px 10px;color:#1e40af">
  {caveat}<br>
  <span style="font-size:11px">口径：{cov.get('note', '—')} · 抓取成功 {ok}/{base} · 含量化增强 {cov.get('n_quant_enhanced', '—')} 只</span>
</div>
{warn}
{kpi}
<div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:14px">
  <div style="flex:1;min-width:320px">
    <div class="kv"><b>行业持仓（绝对值）</b></div>
    <div class="table-scroll"><table><thead><tr><th>行业</th><th class="num">持仓(亿)</th><th class="num">股票数</th><th class="num">占流通</th></tr></thead>
    <tbody>{_ind_rows(p.get('top_industries')) or '<tr><td colspan="4">—</td></tr>'}</tbody></table></div>
  </div>
  <div style="flex:1;min-width:320px">
    <div class="kv"><b>个股持仓占流通比 Top10</b><span style="color:#90a4ae">（小盘更易上榜）</span></div>
    <div class="table-scroll"><table><thead><tr><th>名称</th><th>代码</th><th>行业</th><th class="num">持仓(亿)</th><th class="num">基金数</th><th class="num">占流通</th></tr></thead>
    <tbody>{_stock_rows(p.get('top_stocks_by_ownership')) or '<tr><td colspan="6">—</td></tr>'}</tbody></table></div>
  </div>
</div>
<div class="kv" style="margin-top:10px;color:#90a4ae">
  这是「公募量化基金季报前十大」的持仓下限，不是全部量化持仓：私募不披露、季报只有前十大、
  前十大约覆盖 50–60% 股票仓位。它适合看长期持有/拥挤，不适合日频交易。
</div>
</section>"""


def render_quick_nav():
    items = [
        ("总览", "sec-today"),
        ("① 国家队", "sec-national"), ("② 要闻", "sec-news"),
        ("③ 信号", "sec-signals"), ("④ V6", "sec-v6"),
        ("⑤ 政策", "sec-policy"), ("⑥ 情绪", "sec-emotion"),
        ("⑦ 浮盈保护", "sec-risk"), ("⑧ 量化活跃度", "sec-quant"),
        ("⑨ 量化持仓", "sec-holdings"), ("指标字典", "sec-glossary"),
    ]
    links = "".join(
        f'<a href="#{i}" style="color:#1a73e8;text-decoration:none;padding:3px 9px;'
        f'border:1px solid #e2e8f0;border-radius:12px;background:#fff">{n}</a>'
        for n, i in items)
    return (f'<div style="display:flex;gap:8px;flex-wrap:wrap;padding:10px 24px;'
            f'background:#f8fafc;border-bottom:1px solid #eee;font-size:12px">'
            f'<span style="color:#607d8b;margin-right:4px">快速跳转：</span>{links}</div>')


def render_today_summary():
    """总分结构的「总」：今天先看三件事，指标解释在字典。"""
    s, _ = load_v6_summary()
    rg, _ = load_risk_summary()
    q, _ = load_quant_activity()
    t = (s or {}).get("temperature") or {}
    risk = (s or {}).get("index_risk") or {}
    pos = (s or {}).get("position") or {}
    card = (s or {}).get("decision_card") or {}
    m = (q or {}).get("market") or {}
    state = rg.get("state") if isinstance(rg, dict) else None
    ok = bool(rg.get("ok", True)) if isinstance(rg, dict) else False
    ex = (rg.get("existing") or {}) if isinstance(rg, dict) else {}
    ne = (rg.get("new_entry") or {}) if isinstance(rg, dict) else {}
    trim = (rg.get("action") or {}).get("initial_trim", 0) if isinstance(rg, dict) else 0

    temp_txt = f"{t.get('temp'):.1f}" if isinstance(t.get("temp"), (int, float)) else "—"
    risk_txt = (f"{risk.get('label')}（{risk.get('score')}/6）"
                if risk else "—")
    guard_txt = (f"{rg.get('state_label')}（{rg.get('score')}/8）" if ok else "风险状态不可用")
    guard_sub = (ex.get("text") or f"初减 {trim:.0%} · 剩余8%止损") if ok else "沿用原纪律"
    ceiling = ne.get("ceiling_text") or "—"
    q_market = m.get("score", "—")
    q_sub = (f"微盘 {m.get('micro_score', '—')} · 大盘 {m.get('large_score', '—')} "
             f"· spread {m.get('spread_score', '—')}")

    if ok and state in ("fragile", "crash"):
        priority = ("先防守：已有浮盈仓按 ⑦ 初减/8%止损；"
                    "新仓受 ⑦ 上限约束；等状态回到警戒/正常再谈进攻。")
    else:
        priority = ("按 V6 温度档与 ⑦ 防守层执行；新仓不超过 ⑦/④ 的上限。")

    return f"""
<section class="section" id="sec-today" style="background:#f8fafc">
  <h2>🧭 今天先看这三件事 <span style="font-size:12px;color:#90a4ae;font-weight:400">总览 · 细节在下方各段</span></h2>
  <div class="flow-grid" style="margin-top:10px">
    {_v6_box("🌡 大盘", f"{temp_txt}<span style='font-size:13px;color:#90a4ae'>/100</span>",
             f"{t.get('level', '—')} · 阶段 {t.get('regime', card.get('regime', '—'))} · 大盘风险 {risk_txt}")}
    {_v6_box("🛡 已有浮盈", guard_txt, guard_sub, "up" if state in ("fragile", "crash") else "")}
    {_v6_box("📡 量化活跃度", f"{q_market}<span style='font-size:13px;color:#90a4ae'>/100</span>",
             q_sub + " · 代理，不是成交占比")}
  </div>
  <div class="kv" style="margin-top:10px"><b>一句话：</b>{priority}</div>
  <div class="kv" style="color:#64748b">看不懂某个数字？先看 <a href="#sec-glossary">指标字典</a>；
    每段的「怎么看/别误读」都在那里。⑦ 的动作只作用于已有浮盈仓；④ 的仓位是进攻/新仓。</div>
</section>"""


def _gloss_table(rows):
    tr = "".join(
        f"<tr><td style='white-space:nowrap'><b>{a}</b></td><td>{b}</td><td>{c}</td>"
        f"<td style='color:#b45309'>{d}</td></tr>" for a, b, c, d in rows)
    return (f"<div class='table-scroll'><table><thead><tr>"
            f"<th>指标</th><th>它是什么（人话）</th><th>怎么读 / 阈值</th>"
            f"<th>别误读</th></tr></thead><tbody>{tr}</tbody></table></div>")


def render_glossary():
    groups = [
        ("① 国家队资金（ETF 份额净申购）", [
            ("ETF 净申购", "国家队今天净买入了多少（份额变化 × 价格）",
             "越大越像在买；>近20日80分位 = 比过去80%的日子都强",
             "不是成交额；成交额含做市/套利换手，会大几十倍"),
            ("主力净流入", "大单资金今天净买入/卖出多少",
             "|z|≥1 = 偏离20日均值超过1个标准差", "按单笔金额分类，不一定是机构"),
            ("分位", "在最近N天里排第几", "80分位 = 比80%的日子高", "分位高≠明天涨"),
        ]),
        ("③ 投资信号（滚动分位）", [
            ("ETF 净申购信号", "净申购达到近20日80分位", "触发=值得关注", "触发不是必买"),
            ("主力资金信号", "主力净流入偏离20日均值≥1σ", "|z|≥1 触发", "单日噪声大"),
        ]),
        ("④ V6 交易决策", [
            ("温度 0–100", "市场热度（拥挤度）", "<45低温=不拥挤；>55高温=过热",
             "低温≠马上涨；是概率/反向指标"),
            ("阶段", "技术性牛熊 + 年线/半年线", "过冷/恐慌、下跌、震荡、过热、上涨",
             "阶段是背景，不是个股买卖指令"),
            ("大盘风险 0–6", "6项技术风险命中几项", "高/极高=多项破位",
             "诊断用；动作看 ⑦ 浮盈保护"),
            ("温度档仓位", "按温度档给新仓的目标数量", "如 3只(半仓)",
             "不是已有仓位的减仓比例"),
            ("决策单阶段仓位", "阶段给的仓位区间", "如 30-50%（试探建仓）",
             "与温度档是两种口径，别混"),
            ("市场广度", "上涨/下跌家数占比", "越低越弱；看趋势不看看单日",
             "单日噪声大"),
            ("250日回撤", "离一年最高点跌了多少", "越深=跌得越多（页面取绝对值）",
             "不是估值便宜"),
            ("P≧20% / 预期", "模型给的上涨概率/预期收益", "概率是排序，不是保证",
             "不要单票满仓"),
        ]),
        ("⑤ 政策信号", [
            ("政策力度分 0–100", "政策动作的强度", "25=形式大于实质；越高越强",
             "不是市场底"),
            ("上限 cap", "政策能带来的资金/力度上限", "8%=有动作但力度不足",
             "不是买入信号"),
            ("市场位置", "当前点位在政策有效区的位置", "有效区/无效区",
             "政策不是万能"),
        ]),
        ("⑥ 情绪温度计", [
            ("涨跌家数比", "上涨家数 / 下跌家数", "<0.5偏弱；<0.25退潮/冰点",
             "退潮后历史反而偏涨，不是卖出信号"),
            ("涨停/跌停", "当日涨停/跌停家数", "跌停≥25=冰点/恐慌",
             "冰点≠继续跌"),
            ("晋级率", "昨日涨停今天继续涨停的比例", "低=赚钱效应差",
             "短线情绪，不是大盘"),
            ("情绪分/恐慌分", "0–100 的热度/恐慌", "越高越热/越恐慌",
             "反向指标，不是买卖指令"),
        ]),
        ("⑦ 浮盈保护（防守层）", [
            ("脆弱度 0–8", "大盘脆弱度打分", "0–1正常、2–3警戒、4–5脆弱、6–8崩跌",
             "动作看状态，不只看分数"),
            ("建议初减", "对已有浮盈仓建议先卖多少", "25% / 40% / 50%",
             "不是目标仓位，也不是清仓"),
            ("8% 移动止损", "从持仓最高收盘价回撤8%就减/退", "峰值不是成本价",
             "不是买入就设8%止损"),
            ("下跌占比", "当日下跌家数 / 全市场", "≥80%=广泛下跌",
             "单日不够，看5日/10日"),
            ("MA20上方", "多少股票在20日均线上方", "<30%弱、<20%更弱",
             "广度，不是指数"),
            ("10日冲击", "10日内80%下跌的天数", "≥2=第二次冲击",
             "第一次冲击没有预测力"),
            ("新仓/总仓上限", "新仓/总仓最多多少", "如≤15%（V6 danger）",
             "不是已有仓位的减仓比例"),
        ]),
        ("⑧ 量化活跃度（代理）", [
            ("市场/微盘/大盘 0–100", "当天活跃度在近60日的分位",
             "越高越活跃；微盘-大盘=背离", "是相对分位，不是绝对成交占比"),
            ("spread", "微盘活跃度 − 大盘活跃度", "100=60日最高背离",
             "高背离=微盘/题材被高频资金做，不是量化持仓"),
            ("换手/振幅/日内/小单占比", "代理的四个输入",
             "换手高、振幅大、日内波动大、小单多=更像高频",
             "小单多也可能是散户"),
            ("代理", "不是量化成交占比", "无法识别单笔交易",
             "真值需 L2/逐笔或付费厂商"),
        ]),
        ("⑨ 量化持仓（季度·公募下限）", [
            ("季度/前十大", "公募量化基金季报前十大持仓", "季度更新",
             "不是日频，不是全部持仓"),
            ("占流通市值", "公募量化前十大持仓 / 流通市值", "约0.1–0.3%量级",
             "下限，不含私募"),
            ("覆盖基金", "抓取成功 / 基础名称去重", "≥80%才展示占比",
             "覆盖不足会显示警告"),
            ("个股占流通比", "基金持有该股 / 该股流通市值", "小盘更容易高",
             "不是量化持仓的全部"),
        ]),
    ]
    blocks = "".join(
        f"<details{' open' if title.startswith(('⑦', '⑧', '⑨')) else ''} "
        f"style='margin:8px 0'><summary style='cursor:pointer;font-weight:600;"
        f"color:#334155'>{title}</summary>{_gloss_table(rows)}</details>"
        for title, rows in groups)
    return f"""
<section class="section" id="sec-glossary">
  <h2>📖 指标字典 <span style="font-size:12px;color:#90a4ae;font-weight:400">
    普通人版 · 每个数字一句话 · 先看结论再看表</span></h2>
  <p style="font-size:13px;color:#475569;line-height:1.7">
    这张表回答三件事：<b>它是什么</b>、<b>阈值怎么读</b>、<b>最容易怎么误读</b>。
    阈值是经验分位/回测值，不是保证；任何单一指标都不该单独决定买卖。
    先看顶部的「今天先看这三件事」，再按需展开下面的分组。</p>
  {blocks}
  <p style="font-size:12px;color:#90a4ae;margin-top:8px">
    更技术性的口径（数据源、公式、降级处理）在下方「口径说明」。</p>
</section>"""


def render_nav():
    """跨看板导航 —— 投资相关内容分散在多个入口，这里给出唯一入口。"""
    v6, _ = load_v6_summary()
    v6_html = ((v6 or {}).get("links") or {}).get("html") or f"{V6_PAGES}/"
    items = [
        ("个人投资看板", "dashboard_investment.html", True),
        ("V6 交易日报", v6_html, False),
        ("政策信号全文", "policy/index.html", False),
        ("财经早报归档", "news/index.html", False),
        ("个人健康", "health/index.html", False),
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
    return (f'<section class="section" id="sec-archive"><h2>历史归档 '
            f'<span style="font-size:12px;color:#90a4ae;font-weight:400">最近 {len(files)} 期 · 每天一份</span></h2>'
            f'<div class="arc-list">{items}</div></section>')


def render_methodology(d):
    tw = d.get('trading_dates_window', ['—', '—']) or ['—', '—']
    off = (d.get('offline_modules') or {}).get('量化集中度')
    return f"""<section class="section" id="sec-method">
<h2>口径说明</h2>
<div class="methodology">
<p><b>国家队资金（v5 已换口径）</b> = 沪市宽基ETF（510050 / 510300 / 510310 / 510330 / 510500 等）<b>份额日变化 × 最新价</b>。
这是汇金/证金申购 ETF 的直接体现。旧版用「ETF成交额」当代理，实测同日成交 147.7亿 而实际净申购仅 0.42亿，差 <b>350 倍</b> ——
成交额绝大部分是做市与套利换手，不是国家队买入，已弃用（成交额仅作背景数字保留）。</p>
<p><b>主力净流入</b> = 东财全市场「超大单 + 大单」净额，按<b>单笔成交金额</b>分类，不代表机构身份；与"国家队"不是同一概念。</p>
<p><b>信号</b> = 滚动分位：ETF净申购 ≥ 近20日80分位 → 国家队大额申购；主力净流入 |z|≥1 → 资金异动。北向停止披露，不计入。</p>
<p><b>数据源</b>：akshare 免费接口（东财 / 上交所 ETF 份额），Hermes cron 工作日 <code>30 18 * * *</code> 日更。历史窗口 {tw[0]} → {tw[-1]}。</p>
<p><b>局限</b>：沪市份额可取历史（交易所按日），深市无历史接口 —— 自 {d.get('etf', {}).get('sz_tracked_since', '—')} 起逐日快照累积（已 {d.get('etf', {}).get('sz_tracked_days', 0)} 日），
首日深市无变化值；估算价用最新价，历史日净申购存在小幅估值误差；主力净流入按单笔金额分类，无法区分量化/机构/游资。</p>
<p><b>④ V6 交易决策</b> = 从 trend-trading-system 的 V6 日报结构化摘要（<code>reports/v6_daily/v6_daily_YYYYMMDD.json</code>，schema=1）读取，只做展示，不在本看板重算。
口径分工：本看板 ① 是<b>资金来源层</b>（ETF 份额净申购 / 主力净额，原始事实），④ 的「国家队进出场信号」是<b>信号判定层</b>（份额×点位×杠杆交叉验证 → ENTRY/EXIT/ROTATE，含置信度）；
两者数据源相同、口径不同，出现方向不一致时以「① 看资金事实、④ 看判定结论」分别理解，不要互相校验。</p>
<p><b>④ 与 ⑦ 分工</b>：④ 是进攻/新仓层（温度档、行业臂、候选股）；⑦ 是防守/已有浮盈层
（脆弱度初减 + 峰值回撤 8% 移动止损）。两层并行，不互相覆盖。已有浮盈仓的动作以 ⑦ 为准；
新仓/总仓上限取 ④ 中脆弱度 danger、Index Risk、阶段仓位三者中最严格者。若 ⑦ 自报数据滞后，
看板会暂停新建仓建议并提示，但不会阻断 ④ 的展示。</p>
<p><b>⑧/⑨ 量化</b>：⑧ 是日频「活跃度代理」——用换手率/振幅/日内波动/小单占比合成，
每日横截面排序，<b>不是量化成交占比</b>，也无法识别单笔交易是否为量化；⑨ 是公募量化基金
季报前十大持仓 / 流通市值，<b>季度更新、仅公募、不含私募，是下限而非全量</b>。
真·量化成交占比需要 L2/逐笔或付费厂商数据，本看板不做。</p>
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
<div class="header"><div><h1>📊 个人投资看板</h1><div class="meta">{d.get('date','')} · 页面渲染于 {d.get('generated_at','—')}（各段实际产出时间见标题行）· v9（九段：V6 / 浮盈保护 / 量化代理）</div></div>{badge}</div>
{render_nav()}
{render_quick_nav()}
{render_today_summary()}
{render_module1(d)}
{render_module2(d)}
{render_module3(d)}
{render_module_v6()}
{render_module_policy()}
{render_module_emotion()}
{render_module_risk()}
{render_module_quant_activity()}
{render_module_quant_holdings()}
{render_archive(theme, base)}
{render_glossary()}
{render_methodology(d)}
<div class="footer">投资看板 · akshare + V6 日报（工作日 02:45）+ 量化活跃度代理/季度持仓 · Hermes cron 工作日 18:30 日更</div>
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
