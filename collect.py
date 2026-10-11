#!/usr/bin/env python3
"""collect.py — 汇集 Hermes 生态的所有监控数据 → data.json

数据源：
  1. Hermes cron   ~/.hermes/cron/jobs.json
  2. 系统 crontab  `crontab -l`
  3. launchd       ~/Library/LaunchAgents/*.plist + launchctl list
  4. Ad-hoc 任务   Obsidian Vault tasks/ + responses/
  5. 盘点 monitor  磁盘 / 备份 / 服务 / vault / 技能
每个采集器独立 try/except，单点失败不拖垮整体。
仅用标准库。
"""
import json, os, re, subprocess, datetime, glob, sqlite3, shutil, hashlib

HOME = os.path.expanduser("~")
VAULT = os.path.join(HOME, "Documents/Obsidian Vault")
HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.datetime.now()

def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def safe(fn, default):
    try:
        return fn()
    except Exception as e:
        return {"_error": str(e)} if isinstance(default, dict) else default

# ── 1. Hermes cron ──────────────────────────────────────────────
def collect_hermes_cron():
    p = os.path.join(HOME, ".hermes/cron/jobs.json")
    if not os.path.exists(p): return []
    raw = json.load(open(p, encoding="utf-8"))
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    out = []
    for j in jobs:
        sched = j.get("schedule") or {}
        out.append({
            "id": j.get("id"), "name": j.get("name"),
            "schedule": j.get("schedule_display") or sched.get("display") or str(sched),
            "last_run": (j.get("last_run_at") or "")[:16].replace("T", " "),
            "last_status": j.get("last_status") or "-",
            "next_run": (j.get("next_run_at") or "")[:16].replace("T", " "),
            "enabled": bool(j.get("enabled")),
            "state": j.get("state"), "deliver": j.get("deliver"),
            "script": j.get("script") or (j.get("prompt") or "")[:60],
            "fail_streak": j.get("failure_streak", 0),
            "last_error": (j.get("last_error") or "")[:120],
        })
    return out

# ── 2. crontab ──────────────────────────────────────────────────
DOW = {"0": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "日"}
def human_cron(expr):
    f = expr.split()
    if len(f) != 5: return expr
    mi, hr, dom, mon, dow = f
    def t(x): return x if x != "*" else "每"
    hm = f"{hr}:{mi.zfill(2)}" if hr != "*" and "," not in hr and "-" not in hr else f"{hr}时{mi}分"
    if mi.startswith("*/"): hm = f"每 {mi[2:]} 分钟"
    elif hr.startswith("*/"): hm = f"每 {hr[2:]} 小时"
    day = ""
    if dow != "*":
        days = "/".join(DOW.get(x, x) for x in re.split(r"[,/-]", dow) if x)
        day = f"周{days}"
    if dom != "*": day = f"{dom}日"
    if mi.startswith("*/") or hr.startswith("*/"): return hm
    return f"{day} {hm}".strip()

def cron_next(expr, after=None):
    """极简 next-run 计算（支持 */n, a-b, 列表, 具体值）。"""
    after = after or NOW
    f = expr.split()
    if len(f) != 5: return ""
    def parse(field, lo, hi):
        vals = set()
        for part in field.split(","):
            if part == "*": vals |= set(range(lo, hi + 1))
            elif part.startswith("*/"):
                st = int(part[2:]); vals |= set(range(lo, hi + 1, st))
            elif "-" in part:
                a, b = part.split("-")[:2]; vals |= set(range(int(a), int(b) + 1))
            else:
                try: vals.add(int(part))
                except: pass
        return sorted(vals) or list(range(lo, hi + 1))
    mins = parse(f[0], 0, 59); hrs = parse(f[1], 0, 23)
    doms = parse(f[2], 1, 31); mons = parse(f[3], 1, 12)
    dows = [d % 7 for d in parse(f[4], 0, 7)]
    t = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(60 * 24 * 40):
        if (t.minute in mins and t.hour in hrs and t.month in mons
                and (t.day in doms or f[2] == "*") and (t.weekday() + 1) % 7 in dows):
            return t.strftime("%Y-%m-%d %H:%M")
        t += datetime.timedelta(minutes=1)
    return ""

def collect_crontab():
    out = []
    for line in sh("crontab -l").splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        m = re.match(r"^([\d*/,\-]+(?:\s+[\d*/,\-]+){4})\s+(.*)$", line)
        if not m: continue
        expr, cmd = m.group(1), m.group(2)
        # 提取日志文件路径
        lm = re.search(r">>\s*([^\s]+\.log)", cmd)
        logf = lm.group(1).replace("\\ ", " ") if lm else ""
        logf = os.path.expanduser(logf) if logf else ""
        last_mtime, tail = "", ""
        if logf and os.path.exists(logf):
            last_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(logf)).strftime("%Y-%m-%d %H:%M")
            tail = sh(f"tail -1 {json.dumps(logf)} 2>/dev/null")[:100]
        out.append({
            "schedule": expr, "human": human_cron(expr), "cmd": cmd[:150],
            "next_run": cron_next(expr), "log": logf.replace(HOME, "~") if logf else "",
            "last_log": last_mtime, "last_line": tail,
        })
    out.sort(key=lambda x: x["next_run"] or "9")
    return out

# ── 3. launchd ──────────────────────────────────────────────────
LAUNCHD_SKIP = ("com.apple.", "com.google.", "io.github.")
def collect_launchd():
    out = []
    for pl in sorted(glob.glob(os.path.join(HOME, "Library/LaunchAgents/*.plist"))):
        label = os.path.basename(pl)[:-6]
        if label.startswith(LAUNCHD_SKIP): continue
        info = sh(f"launchctl list {label} 2>/dev/null")
        pid = re.search(r'"PID"\s*=\s*(\d+)', info)
        st = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', info)
        prog = re.search(r'"Program"\s*=\s*"([^"]+)"', info) or re.search(r"ProgramArguments.*?\n\s*\"([^\"]+)\"", info, re.S)
        out.append({
            "label": label, "pid": pid.group(1) if pid else "",
            "status": "running" if pid else "idle",
            "last_exit": st.group(1) if st else "-",
            "program": (prog.group(1) if prog else "")[:80],
            "schedule": "常驻/KeepAlive" if "KeepAlive" in open(pl, encoding="utf-8", errors="replace").read() else "按需",
        })
    return out

# ── 4. Ad-hoc 任务 ──────────────────────────────────────────────
THEMES = [
    ("投资研究", ["估值", "A股", "a股", "股票", "量化", "交易", "回测", "财报", "tdd", "行情", "个股", "行业", "策略", "tushare", "trend", "601138", "300394", "688102", "用友", "富联", "天孚", "量子", "三花", "金山", "免税", "中免",
                 "波段", "v6", "V6", "日报", "回撤", "动量", "引擎", "因子", "ic", "IC", "特征工程", "训练", "选股", "温度计", "大盘", "ml", "ML", "收益", "仓位", "止损", "反转", "指数"]),
    ("职业发展", ["面试", "interview", "简历", "求职", "职场", "prep", "career", "跳槽", "resume", "offer", "招聘"]),
    ("旅行/行程", ["visa", "签证", "行程", "景点", "旅行", "退票", "itinerary", "机票", "酒店"]),
    ("知识库/文档", ["obsidian", "vault", "知识库", "标签", "笔记", "文档", "归档", "索引", "mirror"]),
    ("Agent 系统", ["hermes", "agent", "skill", "技能", "cron", "网关", "gateway", "bot", "claude", "派发", "dispatch", "protocol", "协议",
                    "ollama", "launchd", "rag", "RAG", "记忆双写", "规范", "留痕", "溯源", "模型路由", "监控看板", "看板", "registry", "推送"]),
    ("健康", ["健康", "health", "ios", "血压", "饮食", "体检", "购粮", "就医", "药品", "养生"]),
    ("图像/视觉", ["图", "comfy", "绘图", "vton", "inpaint", "视觉", "image"]),
    ("播客/媒体", ["播客", "podcast", "小宇宙", "音频", "转录"]),
    ("资讯/新闻", ["新闻", "资讯", "news", "要闻", "快讯"]),
]
def theme_of(text):
    t = text.lower()
    for name, kws in THEMES:
        if any(k.lower() in t for k in kws): return name
    return "其它"

def collect_adhoc():
    items = []
    for path in glob.glob(os.path.join(VAULT, "tasks/*.md")) + glob.glob(os.path.join(VAULT, "responses/*.md")):
        bn = os.path.basename(path)
        # 跳过模板 / 占位文件
        if bn.startswith(".") or re.match(r"^TASK-YYYYMMDD", bn) or bn in ("TASK.md", "TEMPLATE.md"):
            continue
        rel = os.path.relpath(path, VAULT)
        st = safe(lambda: os.stat(path), None)
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d") if st else ""
        txt = open(path, encoding="utf-8", errors="replace").read()[:3000]
        # 标题
        h = re.search(r"^#\s+(.+)$", txt, re.M)
        title = h.group(1).strip() if h else os.path.basename(path)[:-3]
        # 日期：文件名 YYYYMMDD 优先
        dm = re.search(r"(20\d{6})", os.path.basename(path))
        date = f"{dm.group(1)[:4]}-{dm.group(1)[4:6]}-{dm.group(1)[6:]}" if dm else mtime
        # 状态
        sm = re.search(r"\[\[status:([a-z_]+)\]\]", txt)
        status = sm.group(1) if sm else "-"
        has_cron = "cron" in txt.lower() and "定时" in txt
        items.append({
            "id": "A" + hashlib.md5(os.path.basename(path).encode()).hexdigest()[:4].upper(),
            "date": date, "title": title[:80], "status": status,
            "theme": theme_of(os.path.basename(path) + " " + title),
            "path": rel, "size": st.st_size if st else 0,
            "scheduled": has_cron,
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    # 私有映射：id → 真实路径（只写 vault，不进公开仓）
    lookup = {it["id"]: {"path": it["path"], "title": it["title"], "date": it["date"]} for it in items}
    json.dump(lookup, open(os.path.join(HERE, "lookup.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    # 按主题聚类
    groups = {}
    for it in items:
        groups.setdefault(it["theme"], []).append(it)
    return {"total": len(items), "groups": [
        {"theme": k, "count": len(v), "items": v} for k, v in
        sorted(groups.items(), key=lambda x: -len(x[1]))
    ]}

# ── 5. 盘点 monitor ─────────────────────────────────────────────
def dir_size(p):
    try: return int(sh(f"du -sk {json.dumps(p)} 2>/dev/null | cut -f1") or 0) * 1024
    except: return 0
def mb(b): return f"{b/1024/1024:.0f} MB" if b < 1024**3 else f"{b/1024**3:.1f} GB"

def collect_monitor():
    m = {}
    # 磁盘
    df = sh("df -k /System/Volumes/Data | tail -1").split()
    m["disk"] = {"total": int(df[1]) if len(df) > 1 else 0, "used": int(df[2]) if len(df) > 2 else 0,
                 "free": int(df[3]) if len(df) > 3 else 0}
    m["disk"]["used_pct"] = round(100 * m["disk"]["used"] / max(m["disk"]["total"], 1), 1)
    m["bigdirs"] = [
        {"name": "Docker.raw", "size": dir_size(os.path.join(HOME, "Library/Containers/com.docker.docker"))},
        {"name": "Ollama models", "size": dir_size(os.path.join(HOME, ".ollama/models"))},
        {"name": "trend-trading-system", "size": dir_size(os.path.join(HOME, "projects/trend-trading-system"))},
        {"name": "cleanup 归档区", "size": dir_size(os.path.join(HOME, ".hermes/_archive/cleanup-20261011"))},
    ]
    # 备份
    brepo = os.path.join(HOME, "backups/obsidian-vault")
    last_commit = sh(f"cd {json.dumps(brepo)} 2>/dev/null && git log -1 --format='%cd|%h|%s' --date=format:'%Y-%m-%d %H:%M'")
    commits = sh(f"cd {json.dumps(brepo)} 2>/dev/null && git rev-list --count HEAD")
    logf = os.path.join(HOME, ".hermes/logs/vault-backup.log")
    m["backup"] = {
        "repo": "stranger971020/obsidian-vault",
        "last_commit": last_commit, "commits": commits,
        "log_age_h": round((NOW.timestamp() - os.path.getmtime(logf)) / 3600, 1) if os.path.exists(logf) else None,
    }
    # 服务
    m["services"] = []
    for name, chk in [("Ollama", "lsof -nP -iTCP:11434 -sTCP:LISTEN | tail -1"),
                      ("Hermes gateway", "launchctl list ai.hermes.gateway 2>/dev/null | grep -o '\"PID\" = [0-9]*'"),
                      ("Telegram bridge", "launchctl list com.telegram-ai-bridge 2>/dev/null | grep -o '\"PID\" = [0-9]*'"),
                      ("ComfyUI", "pgrep -f 'ComfyUI/main.py' | head -1")]:
        r = sh(chk)
        m["services"].append({"name": name, "state": "up" if r else "down", "detail": r[:50]})
    # Ollama 绑定/暴露
    bind = sh("lsof -nP -iTCP:11434 -sTCP:LISTEN | tail -1 | awk '{print $9}'")
    m["ollama_bind"] = bind or "not listening"
    m["firewall"] = sh("/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null")
    # vault
    vfiles = int(sh(f"find -L {json.dumps(VAULT)} -type f -name '*.md' 2>/dev/null | wc -l") or 0)
    mirror = os.path.join(HOME, "hermes-agent/data/vault_mirror")
    mfiles = int(sh(f"find {json.dumps(mirror)} -type f -name '*.md' 2>/dev/null | wc -l") or 0)
    m["vault"] = {"md_files": vfiles, "mirror_md": mfiles, "mirror_gap": max(0, vfiles - mfiles),
                  "icloud_evicted": int(sh(f"find -L {json.dumps(VAULT)} -name '*.icloud' 2>/dev/null | wc -l") or 0),
                  "conflicts": int(sh(f"find -L {json.dumps(VAULT)} -name '*conflict*' 2>/dev/null | wc -l") or 0)}
    # 技能
    sk = sh("hermes curator status 2>&1")
    def _g(pat, dflt="0"):
        m = re.search(pat, sk)
        return m.group(1) if m else dflt
    m["skills"] = {
        "managed": _g(r"curator-managed skills:\s*(\d+)"),
        "stale": _g(r"\bstale\s+(\d+)"),
        "unmanaged": _g(r"unmanaged \(no provenance marker\):\s*(\d+)"),
    }
    # Hermes cron 失败
    hc = safe(collect_hermes_cron, [])
    m["cron_failing"] = [j for j in hc if isinstance(j, dict) and j.get("last_status") not in ("ok", "-")]
    return m

# ── main ────────────────────────────────────────────────────────
def main():
    data = {
        "generated_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "host": sh("hostname") or "mac",
        "scheduled": {
            "hermes": safe(collect_hermes_cron, []),
            "crontab": safe(collect_crontab, []),
            "launchd": safe(collect_launchd, []),
        },
        "adhoc": safe(collect_adhoc, {"total": 0, "groups": []}),
        "monitor": safe(collect_monitor, {}),
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = len(data["scheduled"]["hermes"]) + len(data["scheduled"]["crontab"]) + len(data["scheduled"]["launchd"])
    print(f"✓ data.json: 定时 {n} 条（hermes {len(data['scheduled']['hermes'])} / crontab "
          f"{len(data['scheduled']['crontab'])} / launchd {len(data['scheduled']['launchd'])}）, "
          f"adhoc {data['adhoc'].get('total',0)} 条")

if __name__ == "__main__":
    main()
