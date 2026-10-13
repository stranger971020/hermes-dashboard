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

Changelog
  2026-10-13  审计 TASK-20261013-001 修复（看板「绿但失真」）：
    · 采集失败不再回落成 0/空 —— 写 COLLECT_ERRORS，render 显示「采集失败」。
      原先 safe() 把崩溃吞成空值，render 把空值当健康默认 → 崩溃与健康同形。
    · 外部命令全走绝对路径 /usr/sbin/lsof、~/.local/bin/hermes —— cron 的
      PATH=/usr/bin:/bin 里没有它们，裸名 = command not found → 假红。
    · 服务探活改 socket（端口通即 up），不再依赖命令行字串（ComfyUI 原先用
      `pgrep -f 'ComfyUI/main.py'` 永不命中：进程名是 `main.py --listen`）。
    · 日志路径解析：支持 `cd <dir>` 前缀（相对路径）与含转义空格的路径 ——
      交易链 4 条 cron（data_refresh / v6_daily_pipeline / v6_weekly_retrain /
      vault_index）原先全部未被纳管，页面永远不判停更。
    · 技能：绝对路径调用；`未纳管`(结构性死字段) 换成 `已归档`（真实存在于输出）；
      采集失败写 None，render 显示「采集失败」而不是 0。
    · 任务状态：全文扫描（原先只读前 3000 字符 → 长文档的状态标签被截掉，
      291 条里 35 条因此显示 `-`）；拆分 digest 阶段与任务生命周期两套状态机。
    · crontab 排程：@daily 等宏展开；DOM/DOW 同时限定时用 OR（与 cron 一致）；
      `1-5` 显示为「周一至周五」（原先 re.split 把 `1-5` 拆成 周一/五）。
    · launchd：未加载（Could not find service）不再与 idle 同形。
    · 推送源 pending：stamp 文件缺失时不再误报「有未推产物」。
    · 备份：记录日志末行（成功/失败由 render 判定并把失败送进告警）。
"""
import json, os, re, subprocess, datetime, glob, socket, hashlib

HOME = os.path.expanduser("~")
VAULT = os.path.join(HOME, "Documents/Obsidian Vault")
HERMES_BIN = os.path.join(HOME, ".local/bin/hermes")
LSOF = "/usr/sbin/lsof"
HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.datetime.now()

# 采集失败台账：safe() 捕获的异常写在这里。render **必须**把它显示成「采集失败」，
# 绝不允许失败回落成 0/空 —— 那会把「没测到」伪装成「没问题」（审计 P1-6）。
COLLECT_ERRORS = {}


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def safe(name, fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        COLLECT_ERRORS[name] = f"{type(e).__name__}: {e}"[:160]
        return default


def port_open(port, host="127.0.0.1", timeout=2):
    """TCP 探活：不依赖 PATH，也不依赖进程命令行字串。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


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
CRON_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
               "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
               "@hourly": "0 * * * *", "@reboot": ""}


def _dow_text(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")[:2]
            out.append(f"{DOW.get(a, a)}至周{DOW.get(b, b)}")   # 1-5 → 周一至周五（不再拆成 一/五）
        else:
            out.append(DOW.get(part, part))
    return "、".join(out)


def human_cron(expr):
    f = expr.split()
    if len(f) != 5: return expr
    mi, hr, dom, mon, dow = f
    hm = f"{hr}:{mi.zfill(2)}" if hr != "*" and "," not in hr and "-" not in hr else f"{hr}时{mi}分"
    if mi.startswith("*/"): hm = f"每 {mi[2:]} 分钟"
    elif hr.startswith("*/"): hm = f"每 {hr[2:]} 小时"
    day = ""
    if dow != "*": day = f"周{_dow_text(dow)}"
    if dom != "*": day = f"{dom}日"
    if mi.startswith("*/") or hr.startswith("*/"): return hm
    return f"{day} {hm}".strip()


def cron_next(expr, after=None):
    """极简 next-run 计算（支持 */n, a-b, 列表, 具体值, @宏）。
    DOM 与 DOW 同时受限时取 OR（cron 语义），不是 AND。"""
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
                except ValueError: pass
        return sorted(vals) or list(range(lo, hi + 1))
    mins = parse(f[0], 0, 59); hrs = parse(f[1], 0, 23)
    doms = parse(f[2], 1, 31); mons = parse(f[3], 1, 12)
    dows = [d % 7 for d in parse(f[4], 0, 7)]
    dom_r, dow_r = f[2] != "*", f[4] != "*"
    t = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(60 * 24 * 40):
        if t.minute in mins and t.hour in hrs and t.month in mons:
            d_ok = (t.weekday() + 1) % 7 in dows
            m_ok = t.day in doms
            if dom_r and dow_r:
                ok = d_ok or m_ok
            elif dom_r:
                ok = m_ok
            elif dow_r:
                ok = d_ok
            else:
                ok = True
            if ok:
                return t.strftime("%Y-%m-%d %H:%M")
        t += datetime.timedelta(minutes=1)
    return ""


def resolve_log(cmd):
    """crontab 命令 → 日志文件绝对路径。

    支持 `>> <path> 2>&1`，path 可含转义空格（`Obsidian\\ Vault`），
    相对路径按命令里出现的 `cd <dir>` 展开（原先只 expanduser → 交易链 4 条全部解析失败）。
    """
    m = re.search(r">>\s*((?:[^\s>'\"]|\\.)+\.log)", cmd)
    if not m:
        return ""
    p = m.group(1).replace("\\ ", " ")
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        cm = re.search(r"(?:^|&&|;|\|)\s*cd\s+((?:[^\s&;|'\"]|\\.)+)", cmd)
        if cm:
            p = os.path.join(os.path.expanduser(cm.group(1).replace("\\ ", " ")), p)
    return p


def collect_crontab():
    out = []
    for line in sh("crontab -l").splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        macro = None
        m = re.match(r"^(@[a-z]+)\s+(.*)$", line)
        if m:
            macro = m.group(1)
            if macro not in CRON_MACROS:
                continue
            expr, cmd = CRON_MACROS[macro], m.group(2)
        else:
            m = re.match(r"^([\d*/,\-]+(?:\s+[\d*/,\-]+){4})\s+(.*)$", line)
            if not m: continue
            expr, cmd = m.group(1), m.group(2)
        logf = resolve_log(cmd)
        last_mtime, tail, size, exists = "", "", 0, False
        if logf and os.path.exists(logf):
            exists = True
            last_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(logf)).strftime("%Y-%m-%d %H:%M")
            size = os.path.getsize(logf)
            tail = sh(f"tail -1 {json.dumps(logf)} 2>/dev/null")[:100]
        # 名称：命令里最后一个 .py/.sh 脚本 basename（无扩展名）
        hits = re.findall(r"([\w.\-]+\.(?:py|sh))\b", cmd)
        name = os.path.splitext(os.path.basename(hits[-1]))[0] if hits else (cmd.split()[0] if cmd else "?")
        out.append({
            "schedule": expr, "macro": macro,
            "human": (f"{macro} " if macro else "") + (human_cron(expr) if expr else ""),
            "cmd": cmd[:150], "name": name,
            "next_run": cron_next(expr) if expr else "",
            "log": logf.replace(HOME, "~") if logf else "",
            "log_exists": exists,
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
        info = sh(f"launchctl list {label} 2>&1")
        pid = re.search(r'"PID"\s*=\s*(\d+)', info)
        st = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', info)
        prog = re.search(r'"Program"\s*=\s*"([^"]+)"', info) or re.search(r"ProgramArguments.*?\n\s*\"([^\"]+)\"", info, re.S)
        if not info or "Could not find service" in info:
            status = "not_loaded"      # 未加载 ≠ 空闲：loaded-but-idle 才会打印属性表
        elif pid:
            status = "running"
        else:
            status = "idle"
        out.append({
            "label": label, "pid": pid.group(1) if pid else "",
            "status": status,
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
# 两套状态机（同一列混在一起读者无法判断含义）
LIFECYCLE = ("completed", "active", "pending", "verified", "superseded", "in_progress",
             "failed", "blocked", "xxx")
DIGEST_STAGE = ("pending_digest", "digested", "awaiting_hermes_review", "acknowledged")


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
        st = safe(f"adhoc/stat/{bn}", lambda: os.stat(path), None)
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d") if st else ""
        # 全文读取：状态标签按 R2 规范置于文末标签块，原先只读前 3000 字符会截掉它
        txt = open(path, encoding="utf-8", errors="replace").read()
        # 标题
        h = re.search(r"^#\s+(.+)$", txt, re.M)
        title = h.group(1).strip() if h else os.path.basename(path)[:-3]
        # 日期：文件名 YYYYMMDD 优先
        dm = re.search(r"(20\d{6})", os.path.basename(path))
        date = f"{dm.group(1)[:4]}-{dm.group(1)[4:6]}-{dm.group(1)[6:]}" if dm else mtime
        # 状态：全文所有 [[status:x]] —— 主列优先取任务生命周期，没有则取 digest 阶段
        # （digest 阶段只在两者都存在时作副标签展示，避免同一列混两套状态机）。
        tags = re.findall(r"\[\[status:([a-z_]+)\]\]", txt)
        life = next((t for t in tags if t in LIFECYCLE), "")
        dig = next((t for t in tags if t in DIGEST_STAGE), "")
        status = life or dig or "-"
        digest = dig if (life and dig) else ""
        has_cron = "cron" in txt.lower() and "定时" in txt
        items.append({
            "id": "A" + hashlib.md5(os.path.basename(path).encode()).hexdigest()[:4].upper(),
            "date": date, "title": title[:80], "status": status, "digest": digest,
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
    except ValueError: return 0


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
    # 备份：库状态 + 日志末行（成败判据交给 render/checks，采集层只如实记录）
    brepo = os.path.join(HOME, "backups/obsidian-vault")
    last_commit = sh(f"cd {json.dumps(brepo)} 2>/dev/null && git log -1 --format='%cd|%h|%s' --date=format:'%Y-%m-%d %H:%M'")
    commits = sh(f"cd {json.dumps(brepo)} 2>/dev/null && git rev-list --count HEAD")
    logf = os.path.join(HOME, ".hermes/logs/vault-backup.log")
    last_line = ""
    if os.path.exists(logf):
        try:
            with open(logf, encoding="utf-8", errors="replace") as fh:
                last_line = (fh.read().strip().splitlines() or [""])[-1][:160]
        except OSError:
            pass
    m["backup"] = {
        "repo": "stranger971020/obsidian-vault",
        "last_commit": last_commit, "commits": commits,
        "last_result": last_line,
        "log_age_h": round((NOW.timestamp() - os.path.getmtime(logf)) / 3600, 1) if os.path.exists(logf) else None,
    }
    # 服务：端口探活（Ollama 11434 / ComfyUI 8188）＋ launchd PID
    m["services"] = []
    for name, kind, target in [("Ollama", "port", 11434),
                               ("Hermes gateway", "launchd", "ai.hermes.gateway"),
                               ("Telegram bridge", "launchd", "com.telegram-ai-bridge"),
                               ("ComfyUI", "port", 8188)]:
        if kind == "port":
            up = port_open(target)
            detail = f"127.0.0.1:{target} LISTEN" if up else f"127.0.0.1:{target} 无监听"
        else:
            r = sh(f"launchctl list {target} 2>/dev/null | grep -o '\"PID\" = [0-9]*'")
            up, detail = bool(r), (r[:50] or "未运行")
        m["services"].append({"name": name, "state": "up" if up else "down", "detail": detail})
    # Ollama 绑定/暴露（绝对路径 lsof；空 = 服务未跑，与「已暴露」区分开）
    bind = sh(f"{LSOF} -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null | tail -1 | awk '{{print $9}}'")
    if not bind and port_open(11434):
        bind = "127.0.0.1:11434"
    m["ollama_bind"] = bind or "not listening"
    m["firewall"] = sh("/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null")
    # vault
    vfiles = int(sh(f"find -L {json.dumps(VAULT)} -type f -name '*.md' 2>/dev/null | wc -l") or 0)
    mirror = os.path.join(HOME, "hermes-agent/data/vault_mirror")
    mfiles = int(sh(f"find {json.dumps(mirror)} -type f -name '*.md' 2>/dev/null | wc -l") or 0)
    m["vault"] = {"md_files": vfiles, "mirror_md": mfiles, "mirror_gap": max(0, vfiles - mfiles),
                  "icloud_evicted": int(sh(f"find -L {json.dumps(VAULT)} -name '*.icloud' 2>/dev/null | wc -l") or 0),
                  "conflicts": int(sh(f"find -L {json.dumps(VAULT)} -name '*conflict*' 2>/dev/null | wc -l") or 0)}
    # 技能（绝对路径 —— cron PATH 里没有 ~/.local/bin，裸名调用 = 恒 0）
    sk = sh(f"{json.dumps(HERMES_BIN)} curator status 2>&1", timeout=30)
    managed = re.search(r"curator-managed skills:\s*(\d+)", sk)
    if not managed:
        m["skills"] = {"managed": None, "stale": None, "archived": None,
                       "error": (sk.strip().splitlines() or ["无输出"])[-1][:120] or "hermes curator status 无输出"}
    else:
        m["skills"] = {
            "managed": managed.group(1),
            "stale": (re.search(r"^\s*stale\s+(\d+)", sk, re.M) or [None, None])[1],
            "archived": (re.search(r"^\s*archived\s+(\d+)", sk, re.M) or [None, None])[1],
            "error": "",
        }
    # Hermes cron 失败
    hc = safe("hermes_cron", collect_hermes_cron, [])
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
            "raw_status": v.get("raw_status", ""), "suppressed": bool(v.get("suppressed")),
            "detail": str(v.get("detail", ""))[:160],
            "at": str(v.get("at", ""))[:16].replace("T", " "),
        })
    order = {"fail": 0, "error": 0, "warn": 1, "ok": 2}
    out.sort(key=lambda x: order.get(x["status"], 3))
    return out


def collect_skills():
    """从 vault 账本 logs/skill-changelog.md 解析 skill/协议变更（近 30 天）。"""
    out = {"entries": [], "recent_objects": [], "total_30d": 0, "last_at": None,
           "by_kind": {"add": 0, "chg": 0, "del": 0}, "error": ""}
    ledger = os.path.join(VAULT, "logs/skill-changelog.md")
    if not os.path.exists(ledger):
        return out
    head_re = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (.+)$")
    cnt_re = re.compile(r"(\d+) 个对象变更（➕(\d+) ✏️(\d+) 🗑(\d+)）")
    obj_re = re.compile(r"^### (➕ 新增|✏️ 更新|🗑 删除) `([^`]+)`")
    try:
        with open(ledger, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        out["error"] = str(exc)[:120]
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
        # 只有「确实存在上一次推送记录、且产物比它更新」才算未推；
        # stamp 缺失（last=None）是「不知道」，不是「有未推产物」（原先恒真 → 假红）。
        pending = bool(last and newest and newest != last)
        if pending and newest:
            p = os.path.join(d, newest)
            if os.path.exists(p):
                age_h = round((NOW.timestamp() - os.path.getmtime(p)) / 3600, 1)
        rows.append({"name": JOB_NAMES.get(jid, jid), "slot": slot, "via": "watchdog",
                     "last": mt, "pending": pending, "age_h": age_h,
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
                    # 真实「事故」按唯一分钟计：一次抖动会连刷多行重试，行数 ≠ 次数
                    out.setdefault("_mins", {}).setdefault(label, set()).add(m.group(1)[:16])
                    if not out["last_at"] or m.group(1) > out["last_at"]:
                        out["last_at"] = m.group(1)
                        out["last"] = ln.strip()[-160:]
                    break
    # 换算：incidents[类] = 唯一分钟数
    incidents = {k: len(v) for k, v in out.pop("_mins", {}).items()}
    out["incidents"] = incidents
    out["incident_total"] = sum(incidents.values())
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
    def _soon(i):
        # days_left==0（今天到期）必须算「7 天内」——`or 99` 会把 0 当成 99（审计 P2-6）
        if i["overdue"] or i.get("status") == "done":
            return False
        d = i.get("days_left")
        return d is not None and d <= 7
    return {"items": items, "overdue": sum(1 for i in items if i.get("overdue")),
            "due_soon": sum(1 for i in items if _soon(i))}


def main():
    data = {
        "generated_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "host": sh("hostname") or "mac",
        "scheduled": {
            "hermes": safe("hermes_cron", collect_hermes_cron, []),
            "crontab": safe("crontab", collect_crontab, []),
            "launchd": safe("launchd", collect_launchd, []),
        },
        "adhoc": safe("adhoc", collect_adhoc, {"total": 0, "groups": []}),
        "checks": safe("checks", collect_checks, []),
        "skill_changes": safe("skill_changes", collect_skills, {}),
        "pushes": safe("pushes", collect_pushes, []),
        "errors": safe("errors", collect_errors, {}),
        "backlog": safe("backlog", collect_backlog, {}),
        "monitor": safe("monitor", collect_monitor, {}),
    }
    data["collect_errors"] = dict(COLLECT_ERRORS)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = len(data["scheduled"]["hermes"]) + len(data["scheduled"]["crontab"]) + len(data["scheduled"]["launchd"])
    print(f"✓ data.json: 定时 {n} 条（hermes {len(data['scheduled']['hermes'])} / crontab "
          f"{len(data['scheduled']['crontab'])} / launchd {len(data['scheduled']['launchd'])}）, "
          f"adhoc {data['adhoc'].get('total',0)} 条")
    if COLLECT_ERRORS:
        print(f"⚠️ 采集失败 {len(COLLECT_ERRORS)}: {', '.join(COLLECT_ERRORS)}")


if __name__ == "__main__":
    main()
