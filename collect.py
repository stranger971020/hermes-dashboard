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
        model = str(j.get("model") or "").strip()
        provider = str(j.get("provider") or "").strip()
        no_agent = bool(j.get("no_agent"))
        err = j.get("last_error") or ""
        st = j.get("last_status") or "-"
        # 健康分类
        if not j.get("enabled"):
            health = "paused"
        elif "drift_skip" in err:
            health = "drift"
        elif "blocked_config" in err or "credential" in err:
            health = "config"
        elif st in ("ok", "-"):
            health = "ok"
        else:
            health = "error"
        # 计费分类
        if no_agent:
            cost = "脚本·无LLM"
        elif not model:
            cost = "⚠️跟随主模型(收费)"
        elif "ollama" in provider.lower() or "11434" in provider:
            cost = "本地·免费"
        else:
            cost = "云端·收费"
        out.append({
            "id": j.get("id"), "name": j.get("name"),
            "schedule": j.get("schedule_display") or sched.get("display") or str(sched),
            "last_run": (j.get("last_run_at") or "")[:16].replace("T", " "),
            "last_status": st,
            "next_run": (j.get("next_run_at") or "")[:16].replace("T", " "),
            "enabled": bool(j.get("enabled")),
            "state": j.get("state"), "deliver": j.get("deliver"),
            "script": j.get("script") or (j.get("prompt") or "")[:60],
            "fail_streak": j.get("failure_streak", 0),
            "last_error": err[:120],
            "provider": provider, "model": model,
            "pinned": bool(model), "no_agent": no_agent,
            "health": health, "cost": cost,
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
        last_mtime, tail, size = "", "", 0
        if logf and os.path.exists(logf):
            last_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(logf)).strftime("%Y-%m-%d %H:%M")
            size = os.path.getsize(logf)
            tail = sh(f"tail -1 {json.dumps(logf)} 2>/dev/null")[:100]
        # 名称：命令里最后一个 .py/.sh 脚本 basename（无扩展名）
        hits = re.findall(r"([\w.\-]+\.(?:py|sh))\b", cmd)
        name = os.path.splitext(os.path.basename(hits[-1]))[0] if hits else (cmd.split()[0] if cmd else "?")
        out.append({
            "schedule": expr, "human": human_cron(expr), "cmd": cmd[:150], "name": name,
            "next_run": cron_next(expr), "log": logf.replace(HOME, "~") if logf else "",
            "last_log": last_mtime, "last_line": tail, "log_size": size,
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
                    "ollama", "launchd", "rag", "RAG", "记忆双写", "规范", "留痕", "溯源", "模型路由", "监控看板", "看板", "registry", "推送",
                    "探针", "r9", "R9", "social-search", "自测", "行为自测"]),
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
def collect_checks():
    """中枢检查体系结果（由 checks.py 写入）。"""
    p = os.path.join(HOME, ".hermes/state/hub_checks.json")
    if not os.path.exists(p): return []
    raw = json.load(open(p, encoding="utf-8"))
    out = []
    for k, v in raw.items():
        out.append({
            "key": k, "name": v.get("name", k), "status": v.get("status", "?"),
            "detail": str(v.get("detail", ""))[:160],
            "at": str(v.get("at", ""))[:16].replace("T", " "),
        })
    order = {"fail": 0, "error": 0, "warn": 1, "ok": 2}
    out.sort(key=lambda x: order.get(x["status"], 3))
    return out


def collect_skills():
    """从 vault 账本 logs/skill-changelog.md 解析 skill/协议变更（近 30 天）。"""
    out = {"entries": [], "recent_objects": [], "total_30d": 0, "last_at": None,
           "by_kind": {"add": 0, "chg": 0, "del": 0}}
    ledger = os.path.join(VAULT, "logs/skill-changelog.md")
    if not os.path.exists(ledger):
        return out
    head_re = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (.+)$")
    cnt_re = re.compile(r"(\d+) 个对象变更（➕(\d+) ✏️(\d+) 🗑(\d+)）")
    obj_re = re.compile(r"^### (➕ 新增|✏️ 更新|🗑 删除) `([^`]+)`")
    try:
        with open(ledger, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    cutoff = (NOW - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    cur = None
    for ln in lines:
        m = head_re.match(ln)
        if m:
            c = cnt_re.search(m.group(2))
            cur = {"at": m.group(1),
                   "count": int(c.group(1)) if c else 0,
                   "add": int(c.group(2)) if c else 0,
                   "chg": int(c.group(3)) if c else 0,
                   "del": int(c.group(4)) if c else 0,
                   "objects": []}
            out["entries"].append(cur)
            continue
        o = obj_re.match(ln)
        if o and cur is not None:
            cur["objects"].append({"kind": o.group(1)[0], "name": o.group(2)})
    # 无计数标题（如手工回填条目）→ 用对象数 + 对象类型兜底
    for e in out["entries"]:
        if not e["count"] and e["objects"]:
            e["count"] = len(e["objects"])
            e["add"] = sum(1 for o in e["objects"] if o["kind"] == "➕")
            e["chg"] = sum(1 for o in e["objects"] if o["kind"] == "✏")
            e["del"] = sum(1 for o in e["objects"] if o["kind"] == "🗑")
    # 近 30 天
    for e in out["entries"]:
        if e["at"] >= cutoff:
            out["total_30d"] += e["count"]
            out["by_kind"]["add"] += e["add"]
            out["by_kind"]["chg"] += e["chg"]
            out["by_kind"]["del"] += e["del"]
    if out["entries"]:
        out["last_at"] = out["entries"][0]["at"]
        seen = set()
        for e in out["entries"][:6]:
            for ob in e["objects"]:
                if ob["name"] not in seen:
                    seen.add(ob["name"])
                    out["recent_objects"].append({"name": ob["name"], "kind": ob["kind"], "at": e["at"]})
    out["entries"] = out["entries"][:12]
    return out


def collect_pushes():
    """推送源登记处 —— 谁在往 Telegram 推、推给谁、上次是否真送达。

    来源：watchdog 的 .last_pushed_filename stamp（4 个 job）+ 各外部推送方的日志。
    「看不见的推送源」是 2026-10-11 审计发现的最大缺口（第 5/6 条推送链）。
    """
    JOB_NAMES = {"a4d70e50199b": "Sina 财经/科技早报", "392103372da5": "《播客值得听》周报",
                 "e95423c20e57": "AI Agent Tools Digest", "9fe6f3c37c9d": "Vault 标签健康"}
    rows = []
    # ① watchdog 管辖的 Hermes cron job
    slots = {"a4d70e50199b": "上午 08:00–12:00", "392103372da5": "下午 15:00–21:00",
             "e95423c20e57": "下午 15:00–21:00", "9fe6f3c37c9d": "仅异常（不受窗口限）"}
    for jid, slot in slots.items():
        d = os.path.join(HOME, ".hermes/cron/output", jid)
        stamp_f = os.path.join(d, ".last_pushed_filename")
        last, files = None, []
        try:
            files = sorted(glob.glob(os.path.join(d, "*.md")))
        except OSError:
            pass
        newest = os.path.basename(files[-1]) if files else None
        try:
            with open(stamp_f, encoding="utf-8") as fh:
                last = fh.read().strip()
        except OSError:
            pass
        mt, age_h = None, None
        if last:
            p = os.path.join(d, last)
            if os.path.exists(p):
                mt = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%m-%d %H:%M")
        if newest and newest != last:
            p = os.path.join(d, newest)
            if os.path.exists(p):
                age_h = round((NOW.timestamp() - os.path.getmtime(p)) / 3600, 1)
        rows.append({"name": JOB_NAMES.get(jid, jid), "slot": slot, "via": "watchdog",
                     "last": mt, "pending": bool(newest and newest != last), "age_h": age_h,
                     "kind": "hermes"})
    # ② trend-trading v6 日报（外部项目，直推同一 bot）
    tl = os.path.join(HOME, "projects/trend-trading-system/logs/v6_pipeline_cron.log")
    v6_last = None
    try:
        with open(tl, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if "Telegram 已推送" in ln:
                    v6_last = ln[:16]
    except OSError:
        pass
    reports = sorted(glob.glob(os.path.join(
        HOME, "projects/trend-trading-system/reports/v6_daily/v6_daily_*.html")))
    v6_report = os.path.basename(reports[-1])[9:17] if reports else None
    rows.append({"name": "V6 交易日报", "slot": "周一–五 02:45（盘前信号）",
                 "via": "@flash971_bot 直推", "last": v6_last,
                 "pending": False, "kind": "external", "extra": f"报告 {v6_report}"})
    # ③ daily_bot_doc_check（原先无条件推；2026-10-11 已加 --no-send 抑制）
    bl = os.path.join(VAULT, "logs/bot-check.log")
    bc_note = "已抑制（--no-send）"
    try:
        with open(bl, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-4000:]
        if "Telegram 发送异常" in tail:
            bc_note = "曾尝试推送并失败（401）→ 已改 --no-send"
    except OSError:
        pass
    rows.append({"name": "Bot 文档健康", "slot": "中枢调度（03:00 / 15:00）",
                 "via": "—", "last": None, "pending": False, "kind": "suppressed",
                 "extra": bc_note})
    return rows


def collect_errors():
    """Hermes 自身运行错误（进程活着 ≠ 链路健康）。

    只看近 24h；按类归档：Telegram 网络 / MarkdownV2 回退 / 流中断 / 其它。
    """
    out = {"window_h": 24, "total": 0, "by_kind": {}, "last": None, "last_at": None}
    since = NOW - datetime.timedelta(hours=out["window_h"])
    pats = [
        ("Telegram 网络", re.compile(r"Telegram.*(Network error|ConnectError|reconnect)", re.I)),
        ("MarkdownV2 回退", re.compile(r"MarkdownV2 parse failed", re.I)),
        ("流中断重试", re.compile(r"Stream drop|EmptyStreamError")),
        ("发送重试", re.compile(r"Network error on send", re.I)),
        ("API 连接错误", re.compile(r"APIConnectionError")),
    ]
    for fn in ("errors.log", "gateway.error.log"):
        p = os.path.join(HOME, ".hermes/logs", fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-4000:]
        except OSError:
            continue
        for ln in lines:
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
            if not m:
                continue
            try:
                ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < since:
                continue
            for label, rx in pats:
                if rx.search(ln):
                    out["by_kind"][label] = out["by_kind"].get(label, 0) + 1
                    out["total"] += 1
                    if not out["last_at"] or m.group(1) > out["last_at"]:
                        out["last_at"] = m.group(1)
                        out["last"] = ln.strip()[-160:]
                    break
    return out


def collect_backlog():
    """挂账 / 到期项（看板此前完全没这一栏）。"""
    p = os.path.join(HOME, ".hermes/state/backlog.json")
    items = []
    try:
        with open(p, encoding="utf-8") as fh:
            items = json.load(fh).get("items", [])
    except (OSError, ValueError):
        pass
    today = NOW.strftime("%Y-%m-%d")
    for it in items:
        due = it.get("due") or ""
        if due and due < today and it.get("status") != "done":
            it["overdue"] = True
        else:
            it["overdue"] = False
        if due:
            try:
                d = (datetime.datetime.strptime(due, "%Y-%m-%d").date()
                     - datetime.date.today()).days
                it["days_left"] = d
            except ValueError:
                it["days_left"] = None
    return {"items": items, "overdue": sum(1 for i in items if i.get("overdue")),
            "due_soon": sum(1 for i in items if (i.get("days_left") or 99) <= 7
                            and not i.get("overdue") and i.get("status") != "done")}


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
        "checks": safe(collect_checks, []),
        "skill_changes": safe(collect_skills, {}),
        "pushes": safe(collect_pushes, []),
        "errors": safe(collect_errors, {}),
        "backlog": safe(collect_backlog, {}),
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
