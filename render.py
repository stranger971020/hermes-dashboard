#!/usr/bin/env python3
"""render.py — data.json → HTML（自包含，无外部依赖）

用法:
  render.py --public   → index.html         脱敏版（公开仓 / GitHub Pages）
  render.py --private  → index-private.html  完整版（仅 vault，不公开）
  （无参数 = private）

脱敏（public）:
  · 任务记录只留 日期/主题/类型/状态/ID —— **丢弃标题与路径**
  · 命令/程序路径中的 /Users/<name> → ~
  · ID = 文件名 md5 前 4 位（稳定）；ID→真实路径的映射只在私有的 lookup.json
"""
import json, os, html, sys, datetime, re

PUBLIC = "--public" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "data.json"), encoding="utf-8"))
e = lambda s: html.escape(str(s if s is not None else ""))

def mask_path(s):
    return re.sub(r"/Users/[^/\s\"']+", "~", str(s))

def kind_of(title, path):
    t = (title + " " + path).lower()
    if re.search(r"简历|求职|面试|职位|offer|招聘|职业|jd|cv|resume|interview|job|career|recruit|hiring|hr\b", t): return "职业发展"
    if re.search(r"去衣|褪衣|脱衣|vton|catvton|换脸|inpaint|图像|绘图|comfy|影片|视频|nude|undress", t): return "图像处理"
    if re.search(r"回测|策略|模型|因子|信号|quant|backtest", t): return "策略/回测"
    if re.search(r"部署|运维|cron|脚本|api|网关|gateway|服务|launchd|daemon", t): return "工程/部署"
    if re.search(r"估值|分析|报告|研究|财报|基本面|swot", t): return "分析报告"
    if re.search(r"归档|索引|标签|文档|笔记|vault|obsidian|知识库", t): return "文档/归档"
    if re.search(r"诊断|排查|错误|修复|故障|debug|log", t): return "诊断"
    if re.search(r"模板|template", t): return "模板"
    return "其它"

def status_pill(s):
    s = str(s).lower()
    cls = "ok" if s in ("ok", "up", "running", "success") else ("err" if s in ("error", "failed", "down", "fail") else "warn")
    return f'<span class="pill {cls}">{e(s)}</span>'

def theme_color(t):
    pal = {"投资研究": "#e8a33d", "知识库/文档": "#4a9eda", "Agent 系统": "#8b7fe8",
           "健康": "#3fb98a", "图像/视觉": "#e07a9a", "播客/媒体": "#c78d3f",
           "资讯/新闻": "#5fb3d4", "其它": "#8a8f98"}
    return pal.get(t, "#8a8f98")

S = D["scheduled"]; M = D["monitor"]; A = D["adhoc"]

# ── 脱敏（公开版）────────────────────────────────────────
_RP = os.path.join(HERE, "redact_rules.json")
RULES = json.load(open(_RP, encoding="utf-8")) if os.path.exists(_RP) else {}
# 本地私有规则（人名等，不入公开仓）
_LP = os.path.join(HERE, "redact_local.json")
if os.path.exists(_LP):
    _loc = json.load(open(_LP, encoding="utf-8"))
    RULES.setdefault("topic_map", {}).update(_loc.get("topic_map") or {})
    RULES["entity_mask"] = (RULES.get("entity_mask") or []) + (_loc.get("entity_mask") or [])
    RULES["person_names"] = (RULES.get("person_names") or []) + (_loc.get("person_names") or [])
_TOPIC = [(re.compile(k, re.I), v) for k, v in (RULES.get("topic_map") or {}).items()]
_MASK = [(re.compile(m["pattern"], re.I), m["replace"]) for m in (RULES.get("entity_mask") or [])]
_NAMES = RULES.get("person_names") or []

def redact_title(t):
    """保留描述性关键词，只替换敏感主题词与实体。"""
    t = str(t)
    for rx, rep in _TOPIC:
        t = rx.sub(rep, t)
    for nm in _NAMES:
        if nm: t = t.replace(nm, "[姓名]")
    for rx, rep in _MASK:
        t = rx.sub(rep, t)
    return t


n_sched = len(S["hermes"]) + len(S["crontab"]) + len(S["launchd"])
failing = len(M.get("cron_failing") or [])
backup_last = (M.get("backup", {}).get("last_commit", "") or "").split("|")
cards = [
    ("定时任务", n_sched, f"Hermes {len(S['hermes'])} · crontab {len(S['crontab'])} · launchd {len(S['launchd'])}"),
    ("失败任务", failing, "最近一次运行非 ok" if failing else "全部正常"),
    ("任务记录", A.get("total", 0), f"{len(A.get('groups', []))} 个主题簇"),
    ("最近备份", (backup_last[0] if backup_last else "未知"), f"{M.get('backup',{}).get('commits','?')} 次提交"),
    ("磁盘已用", f"{M.get('disk',{}).get('used_pct','?')}%", f"可用 {M.get('disk',{}).get('free',0)//1024//1024} GB"),
    ("Ollama 绑定", M.get("ollama_bind", "?"), "仅本机 ✅" if "127.0.0.1" in str(M.get("ollama_bind")) else "⚠️ 检查暴露"),
]

# ── Tab1 ──
def tbl_hermes(rows):
    if not rows: return '<p class="empty">无 Hermes cron 任务</p>'
    h = '<table><thead><tr><th>任务</th><th>排程</th><th>模型 / 计费</th><th>上次运行</th><th>状态</th><th>下次运行</th><th>投递</th></tr></thead><tbody>'
    for r in rows:
        md = r.get("model") or "（跟随主模型）"
        cost = r.get("cost") or "—"
        cost_cls = "ok" if "免费" in cost else ("warn" if "跟踪" in cost or "跟随" in cost else "")
        h += (f'<tr><td><b>{e(r["name"])}</b></td>'
              f'<td class="mono">{e(r["schedule"])}</td>'
              f'<td class="mono small">{e(md)}<br><span class="pill {cost_cls}">{e(cost)}</span></td>'
              f'<td class="mono">{e(r["last_run"])}</td>'
              f'<td>{status_pill(r["last_status"])}'
              + (f'<div class="sub" style="color:#f0708a">{(r.get("last_error") or "")[:70]}</div>' if r.get("health") in ("drift","config","error") else "")
              + f'</td><td class="mono">{e(r["next_run"])}</td>'
              f'<td>{e(r["deliver"])}</td></tr>')
    return h + '</tbody></table>'

def tbl_crontab(rows):
    if not rows: return '<p class="empty">无 crontab 任务</p>'
    h = '<table><thead><tr><th>排程</th><th>下次运行</th><th>任务</th><th>命令</th><th>日志最近写入</th></tr></thead><tbody>'
    for r in rows:
        stale = ""
        if r["last_log"]:
            try:
                age = (datetime.datetime.now() - datetime.datetime.strptime(r["last_log"], "%Y-%m-%d %H:%M")).days
                if age > 3: stale = f' <span class="pill warn">停更 {age}天</span>'
            except Exception: pass
        name = os.path.basename(re.split(r"&&|;", r["cmd"])[-1].split()[-1]) if r["cmd"] else "—"
        cmd = mask_path(r["cmd"])[:80]
        h += (f'<tr><td class="mono">{e(r["human"])}<div class="sub mono">{e(r["schedule"])}</div></td>'
              f'<td class="mono">{e(r["next_run"])}</td><td class="mono small">{e(name)}</td>'
              f'<td class="mono small">{e(cmd)}</td><td class="mono small">{e(r["last_log"] or "—")}{stale}</td></tr>')
    return h + '</tbody></table>'

def tbl_launchd(rows):
    if not rows: return '<p class="empty">无 launchd 任务</p>'
    h = '<table><thead><tr><th>Label</th><th>类型</th><th>状态</th><th>PID</th><th>上次退出码</th></tr></thead><tbody>'
    for r in rows:
        h += (f'<tr><td class="mono">{e(r["label"])}</td><td>{e(r["schedule"])}</td>'
              f'<td>{status_pill(r["status"])}</td><td class="mono">{e(r["pid"] or "—")}</td>'
              f'<td class="mono">{e(r["last_exit"])}</td></tr>')
    return h + '</tbody></table>'

# ── Tab2 ──
adhoc_html = ""
for g in A.get("groups", []):
    adhoc_html += (f'<div class="group"><h3><span class="dot" style="background:{theme_color(g["theme"])}"></span>'
                   f'{e(g["theme"])} <span class="cnt">{g["count"]}</span></h3>')
    if PUBLIC:
        adhoc_html += '<table><thead><tr><th class="ck"></th><th>日期</th><th>标题（脱敏）</th><th>类型</th><th>状态</th><th>ID</th></tr></thead><tbody>'
        for it in g["items"]:
            adhoc_html += (f'<tr><td><input type="checkbox" class="pick" data-ref="{e(it["id"])}"></td>'
                           f'<td class="mono">{e(it["date"])}</td><td>{e(redact_title(it["title"]))}</td>'
                           f'<td>{e(kind_of(it["title"], it["path"]))}</td>'
                           f'<td>{status_pill(it["status"])}</td><td class="mono small">{e(it["id"])}</td></tr>')
    else:
        adhoc_html += '<table><thead><tr><th class="ck"></th><th>日期</th><th>标题</th><th>状态</th><th>路径</th><th>ID</th></tr></thead><tbody>'
        for it in g["items"]:
            adhoc_html += (f'<tr><td><input type="checkbox" class="pick" data-ref="{e(it["id"])}"></td>'
                           f'<td class="mono">{e(it["date"])}</td><td>{e(it["title"])}</td>'
                           f'<td>{status_pill(it["status"])}</td><td class="mono small">{e(it["path"])}</td>'
                           f'<td class="mono small">{e(it["id"])}</td></tr>')
    adhoc_html += '</tbody></table></div>'

# ── Tab3 ──
def bar(label, size, maxsize):
    pct = min(100, 100 * size / max(maxsize, 1))
    gb = f"{size/1024**3:.1f} GB" if size >= 1024**3 else f"{size/1024/1024:.0f} MB"
    return f'<div class="barrow"><span class="barlbl">{e(label)}</span><div class="bartrack"><div class="barfill" style="width:{pct:.1f}%"></div></div><span class="barval">{gb}</span></div>'

bd = M.get("bigdirs", []); mx = max([b["size"] for b in bd] or [1])
svc = "".join(f'<div class="svc">{status_pill(s["state"])} <b>{e(s["name"])}</b> <span class="sub">{e(s["detail"])}</span></div>' for s in M.get("services", []))
CK = D.get("checks", [])
_ckpill = {"ok": "ok", "warn": "warn", "fail": "err", "error": "err"}
checks_html = "".join(
    f'<div class="svc"><span class="pill {_ckpill.get(c["status"],"warn")}">{e(c["status"])}</span> '
    f'<b>{e(c["name"])}</b> <span class="sub">{e(c["detail"])}</span>'
    f'<div class="sub mono">{e(c["at"])}</div></div>'
    for c in CK
) or '<p class="empty">尚未运行</p>'
V = M.get("vault", {}); SK = M.get("skills", {})
SC = D.get("skill_changes", {})
_obpill = {"➕": "ok", "✏": "warn", "🗑": "err"}
sc_rows = "".join(
    f'<div class="svc"><span class="pill {_obpill.get(o["kind"],"warn")}">{e(o["kind"])}</span> '
    f'<b>{e(o["name"])}</b> <span class="sub mono">{e((o["at"] or "")[5:])}</span></div>'
    for o in SC.get("recent_objects", [])[:8]
) or '<p class="empty">尚无变更记录</p>'
sc_kind = SC.get("by_kind", {})
sc_card = f"""<div class="mcard"><h4>📚 Skill/协议变更</h4>
    <div class="kv"><span>近 30 天</span><b>{SC.get('total_30d', 0)} 项</b></div>
    <div class="kv"><span>➕ 新增 / ✏️ 更新 / 🗑 删除</span><b>{sc_kind.get('add', 0)} / {sc_kind.get('chg', 0)} / {sc_kind.get('del', 0)}</b></div>
    <div class="kv"><span>最近一次</span><b class="mono">{e(SC.get('last_at') or '—')}</b></div>
    {sc_rows}</div>"""

# ── 📡 推送源登记 + 上次送达 ──────────────────────────────────────────────
PU = D.get("pushes", [])
_pu_pill = {"hermes": "ok", "external": "warn", "suppressed": "warn"}
pu_rows = "".join(
    f'<div class="svc"><span class="pill {_pu_pill.get(p["kind"],"warn")}">{e(p["kind"])}</span> '
    f'<b>{e(p["name"])}</b> <span class="sub">{e(p["slot"])}</span>'
    f'<div class="sub mono">{e(p.get("extra") or "")} '
    f'{"· ⚠️ 有未推产物" if p.get("pending") else "· 已送达 " + e(p.get("last") or "—")}</div></div>'
    for p in PU
) or '<p class="empty">无数据</p>'
pu_card = (f'<div class="mcard"><h4>📡 推送源登记 ({len(PU)})</h4>{pu_rows}'
           f'<div class="sub">「看不见的推送源」是 2026-10-11 审计最大缺口；'
           f'此处按登记处全量列出，含外部项目直推。</div></div>')

# ── 🧯 Hermes 自身运行错误（进程活着 ≠ 链路健康）────────────────────────
ER = D.get("errors", {})
_er_kind = ER.get("by_kind", {})
er_rows = "".join(f'<div class="kv"><span>{e(k)}</span><b>{v} 次 / {ER.get("incidents", {}).get(k, v)} 分</b></div>'
                  for k, v in sorted(_er_kind.items(), key=lambda x: -x[1])) or \
          '<div class="kv"><span>近 24h</span><b>0</b></div>'
er_card = (f'<div class="mcard"><h4>🧯 Hermes 运行错误（近 {ER.get("window_h", 24)}h）</h4>'
           f'<div class="kv"><span>真实事故（按唯一分钟）</span><b>{ER.get("incident_total", ER.get("total", 0))}</b></div>'
           f'<div class="kv"><span>日志行数（含重试刷屏）</span><b>{ER.get("total", 0)}</b></div>{er_rows}'
           f'<div class="sub">行数 ≫ 事故数是正常的：一次网络抖动会连刷多行重试。</div>'
           f'<div class="sub mono">最近 {e(ER.get("last_at") or "—")}</div></div>')

# ── 📌 挂账 / 到期 ────────────────────────────────────────────────────────
BL = D.get("backlog", {})
bl_rows = ""
for it in BL.get("items", []):
    st = it.get("status", "")
    pill = "err" if it.get("overdue") else ("warn" if (it.get("days_left") or 99) <= 7 else "ok")
    due = it.get("due") or ""
    tail = (f'{it.get("days_left")} 天' if due and it.get("days_left") is not None and not it.get("overdue")
            else ("已逾期" if it.get("overdue") else e(st)))
    bl_rows += (f'<div class="svc"><span class="pill {pill}">{e(st)}</span> '
                f'<b>{e(it.get("item", "")[:40])}</b> <span class="sub mono">{e(due)} {tail}</span></div>')
bl_rows = bl_rows or '<p class="empty">无挂账</p>'
bl_card = (f'<div class="mcard"><h4>📌 挂账 / 到期 ({len(BL.get("items", []))})</h4>{bl_rows}</div>')
monitor_html = f"""
<div class="mgrid">
  <div class="mcard"><h4>🩺 中枢检查 ({len(CK)})</h4>{checks_html}</div>
  <div class="mcard"><h4>磁盘占用</h4>{''.join(bar(b['name'], b['size'], mx) for b in bd)}</div>
  <div class="mcard"><h4>服务</h4>{svc}
      <div class="kv"><span>Ollama 绑定</span><b class="mono">{e(M.get('ollama_bind'))}</b></div>
      <div class="kv"><span>防火墙</span><b>{e(M.get('firewall'))}</b></div></div>
  <div class="mcard"><h4>Vault / 备份</h4>
      <div class="kv"><span>vault .md 文件</span><b>{V.get('md_files','?')}</b></div>
      <div class="kv"><span>镜像缺口</span><b>{V.get('mirror_gap','?')}</b></div>
      <div class="kv"><span>iCloud 未下载</span><b>{V.get('icloud_evicted','?')}</b></div>
      <div class="kv"><span>冲突文件</span><b>{V.get('conflicts','?')}</b></div>
      <div class="kv"><span>最近备份</span><b class="mono">{e(backup_last[0] if backup_last else '—')}</b></div>
      <div class="kv"><span>备份提交数</span><b>{M.get('backup',{}).get('commits','?')}</b></div></div>
  <div class="mcard"><h4>技能</h4>
      <div class="kv"><span>纳管</span><b>{SK.get('managed','?')}</b></div>
      <div class="kv"><span>待清理(stale)</span><b>{SK.get('stale','?')}</b></div>
      <div class="kv"><span>未纳管</span><b>{SK.get('unmanaged','?')}</b></div>
      <div class="kv"><span>失败 cron</span><b>{failing}</b></div></div>
  {sc_card}
  {pu_card}
  {er_card}
  {bl_card}
</div>
"""

tip = ("勾选条目 → 底部生成指令，复制发给我即可（归档不删除）。公开版仅显示 主题/类型/ID；"
       "完整标题与路径见私有版 index-private.html。") if PUBLIC else \
      "勾选条目 → 底部生成指令，复制发给我即可（归档不删除）。本页为完整版，含真实标题与路径。"
ver = "公开版" if PUBLIC else "完整版"

HTML = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes 监控看板 · {ver}</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;font:14px/1.55 -apple-system,"PingFang SC",Segoe UI,sans-serif;background:#0f1115;color:#e6e8ec}}
header{{padding:18px 24px 0}} h1{{margin:0;font-size:20px}} .meta{{color:#8a8f98;font-size:12px;margin-top:4px}}
.cards{{display:flex;gap:10px;flex-wrap:wrap;padding:14px 24px}}
.card{{background:#171a21;border:1px solid #232733;border-radius:10px;padding:10px 14px;min-width:120px}}
.card .v{{font-size:20px;font-weight:700}} .card .l{{color:#8a8f98;font-size:11px}} .card .s{{color:#6f7682;font-size:11px;margin-top:2px}}
nav{{display:flex;gap:4px;padding:0 24px;border-bottom:1px solid #232733;position:sticky;top:0;background:#0f1115;z-index:5}}
nav button{{background:none;border:none;color:#8a8f98;padding:12px 16px;font-size:14px;cursor:pointer;border-bottom:2px solid transparent}}
nav button.on{{color:#e6e8ec;border-bottom-color:#4a9eda}}
main{{padding:18px 24px 70px}} .tab{{display:none}} .tab.on{{display:block}}
h2{{font-size:15px;color:#aeb4bf;margin:22px 0 8px;text-transform:uppercase;letter-spacing:.5px}}
h3{{font-size:14px;margin:18px 0 6px;display:flex;align-items:center;gap:8px}} .cnt{{color:#6f7682;font-weight:400}}
table{{width:100%;border-collapse:collapse;background:#141822;border-radius:8px;overflow:hidden}}
th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #1d2230;vertical-align:top}}
th{{background:#1a1f2b;color:#8a8f98;font-weight:600;font-size:12px}}
tr:hover td{{background:#181d28}} .mono{{font-family:ui-monospace,SF Mono,Menlo,monospace;font-size:12px}} .small{{font-size:11px;color:#9aa1ad}}
.sub{{color:#6f7682;font-size:11px}} .empty{{color:#6f7682}}
.pill{{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}}
.pill.ok{{background:#12301f;color:#4ad48a}} .pill.err{{background:#3a1620;color:#f0708a}} .pill.warn{{background:#3a2f14;color:#e8b552}}
.dot{{width:9px;height:9px;border-radius:50%;display:inline-block}}
.pick{{cursor:pointer}} .ck{{width:28px}}
.mgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}}
.mcard{{background:#141822;border:1px solid #1d2230;border-radius:10px;padding:14px}} .mcard h4{{margin:0 0 10px;font-size:13px;color:#aeb4bf}}
.kv{{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1a1f2b;font-size:13px}} .kv span{{color:#8a8f98}}
.barrow{{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px}} .barlbl{{width:130px;color:#aeb4bf}}
.bartrack{{flex:1;height:8px;background:#1d2230;border-radius:4px;overflow:hidden}} .barfill{{height:100%;background:linear-gradient(90deg,#4a9eda,#8b7fe8)}} .barval{{width:70px;text-align:right;color:#8a8f98}}
.svc{{padding:4px 0;border-bottom:1px solid #1a1f2b}}
#toolbar{{position:fixed;bottom:0;left:0;right:0;background:#171a21;border-top:1px solid #232733;padding:10px 24px;display:none;gap:12px;align-items:center}}
#toolbar.on{{display:flex}} #toolbar button{{background:#2b5c8a;border:none;color:#fff;padding:7px 14px;border-radius:6px;cursor:pointer;font-size:13px}}
#toolbar code{{color:#8ad0a0;font-size:12px}}
</style></head><body>
<header><h1>🛰️ Hermes 监控看板 <span class="sub">· {ver}</span></h1>
<div class="meta">生成于 {e(D['generated_at'])} · 数据源：Hermes cron · crontab · launchd · vault</div></header>
<div class="cards">{''.join(f'<div class="card"><div class="l">{e(l)}</div><div class="v">{e(v)}</div><div class="s">{e(s)}</div></div>' for l,v,s in cards)}</div>
<nav>
  <button class="on" data-t="t1">定时任务 ({n_sched})</button>
  <button data-t="t2">任务记录 ({A.get('total',0)})</button>
  <button data-t="t3">系统盘点</button>
</nav>
<main>
  <section class="tab on" id="t1">
    <h2>Hermes cron</h2>{tbl_hermes(S['hermes'])}
    <h2>系统 crontab</h2>{tbl_crontab(S['crontab'])}
    <h2>launchd</h2>{tbl_launchd(S['launchd'])}
  </section>
  <section class="tab" id="t2">
    <p class="meta">{e(tip)}</p>
    {adhoc_html}
  </section>
  <section class="tab" id="t3">{monitor_html}</section>
</main>
<div id="toolbar"><span>已选 <b id="nsel">0</b> 项</span><button id="cpbtn">复制归档指令</button><code id="cpout"></code></div>
<script>
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); document.getElementById(b.dataset.t).classList.add('on');
}});
const tb=document.getElementById('toolbar'), ns=document.getElementById('nsel');
function upd(){{ const s=[...document.querySelectorAll('.pick:checked')];
  ns.textContent=s.length; tb.classList.toggle('on', s.length>0); }}
document.addEventListener('change',ev=>{{ if(ev.target.classList.contains('pick')) upd(); }});
document.getElementById('cpbtn').onclick=()=>{{
  const s=[...document.querySelectorAll('.pick:checked')].map(c=>c.dataset.ref);
  const txt='请归档以下 '+s.length+' 条记录（归档不删除）：'+String.fromCharCode(10)+s.map(p=>'- '+p).join(String.fromCharCode(10));
  navigator.clipboard.writeText(txt).then(()=>{{document.getElementById('cpout').textContent='已复制 '+s.length+' 条';}});
}};
</script></body></html>"""

out = os.path.join(HERE, "index.html" if PUBLIC else "index-private.html")
open(out, "w", encoding="utf-8").write(HTML)
print(f"✓ {os.path.basename(out)}: {len(HTML)} chars")
