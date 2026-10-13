#!/usr/bin/env python3
"""checks.py — 中枢「检查体系」统一入口。

替代 4 个独立 cron 检查任务（intraday / fd / bot-health / cron 配置），
由中枢按内部到期规则调度，结果统一写 state/hub_checks.json，
供看板 + 晨报消费；fail 时立即推 TG 告警。

用法:
  checks.py --due        # 跑所有到期检查（由 run.sh 每 30 分钟调用）
  checks.py --all        # 强制跑全部
  checks.py --list       # 只打印当前状态
"""
import datetime
import json
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
PY = "/Users/jren/miniforge3/bin/python3"
STATE = os.path.join(HOME, ".hermes/state/hub_checks.json")
TT = os.path.join(HOME, "projects/trend-trading-system")

CHECKS = [
    {
        "key": "fd", "name": "Gateway FD / 代理",
        "cmd": [PY, os.path.join(HOME, "projects/obsidian-hermes/scripts/gateway_fd_monitor.py"), "--no-send"],
        "every": 30, "timeout": 60,
    },
    {
        "key": "bots", "name": "Bot 文档健康",
        "cmd": [PY, os.path.join(HOME, "projects/obsidian-hermes/scripts/daily_bot_doc_check.py"), "--health-only", "--no-send"],
        "every": 720, "hours": [3, 15], "timeout": 600, "cap": "warn",
    },
    {
        "key": "bots_full", "name": "Bot 全量扫描",
        "cmd": [PY, os.path.join(HOME, "projects/obsidian-hermes/scripts/daily_bot_doc_check.py"), "--no-send"],
        "every": 1380, "hours": [3], "timeout": 900, "cap": "warn",
    },
    {
        "key": "intraday", "name": "盘中 bot 启动",
        "cmd": [PY, os.path.join(TT, "backtest/check_intraday_bot_startup.py")],
        "every": 30, "hours": [9, 10], "weekdays": True, "timeout": 120,
    },
    {
        "key": "cron", "name": "Cron 模型/配置",
        "builtin": "cron", "every": 60, "timeout": 30,
    },
    {
        "key": "egress", "name": "出网/代理端口",
        "builtin": "egress", "every": 30, "timeout": 30,
    },
    {
        "key": "provider_wiring", "name": "Provider 接线",
        "builtin": "provider_wiring", "every": 360, "timeout": 90,
    },
    # 2026-10-13 审计 TASK-20261013-001：Vault 备份自建立起 100% 失败而看板报绿。
    # 把「备份」从「没人守的日志」变成常驻检查：同时断言接线（cron 用已授权解释器）与结果（日志新鲜无 ERROR）。
    {
        "key": "vault_backup", "name": "Vault 备份",
        "builtin": "vault_backup", "every": 360, "timeout": 30,
    },
]


def _load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)


def _due(chk, now, st):
    prev = st.get(chk["key"], {})
    try:
        last = datetime.datetime.fromisoformat(prev["at"]).timestamp()
    except Exception:
        last = 0
    if now.timestamp() - last < chk.get("every", 1440) * 60:
        return False
    if "hours" in chk and now.hour not in chk["hours"]:
        return False
    if chk.get("weekdays") and now.weekday() > 4:
        return False
    return True


def parse(name, out, rc):
    """把各检查的 stdout 归一成 (status, detail)。"""
    out = out or ""
    if rc != 0:
        return "error", f"exit={rc} {out.strip()[-100:]}"
    m = re.search(r"status=(\w+)", out)
    if "fd=" in out and m:
        return ("ok" if m.group(1) == "ok" else "warn"), out.strip().splitlines()[-1][:90]
    m = re.search(r"健康(?:检查)?[:：]\s*(\d+)/(\d+)\s*\(([\d.]+)%\)", out)
    if m:
        passed, total, pct = int(m.group(1)), int(m.group(2)), float(m.group(3))
        bad = total - passed
        st = "ok" if bad == 0 else ("warn" if pct >= 90 else "fail")
        # 口径：这些是**子项**，检查组的数量是另一回事（原先写「12 项失败」与
        # 「1 个 warn 检查」同屏并存，读者无法判断是 1 个还是 12 个问题）
        return st, f"{passed}/{total} 通过 ({pct}%)，{bad} 个子项失败"
    if "非交易日" in out:
        return "ok", "非交易日，跳过"
    if "启动检查" in out or "预热" in out:
        ok = "✅" in out or "成功" in out
        return ("ok" if ok else "fail"), out.strip().splitlines()[-1][:90]
    return "ok", (out.strip().splitlines()[-1][:90] if out.strip() else "silent")


def check_cron():
    """内置：Hermes cron 健不健康（漂移 / 未 pin / 失败）。"""
    p = os.path.join(HOME, ".hermes/cron/jobs.json")
    raw = json.load(open(p, encoding="utf-8"))
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    problems = []
    for j in jobs:
        if j.get("no_agent") or not j.get("enabled"):
            continue
        err = j.get("last_error") or ""
        model = str(j.get("model") or "").strip()
        if not model:
            problems.append(f"{j.get('name')}: 未 pin（跟随主模型）")
        elif "drift_skip" in err or "blocked_config" in err or "credential" in err:
            problems.append(f"{j.get('name')}: {err[:50]}")
    if problems:
        return "warn", " | ".join(problems[:3])
    return "ok", f"{len([j for j in jobs if not j.get('no_agent')])} 个 LLM job 已 pin"


def check_egress():
    """内置：本地代理端口是否可用（出网总闸）。

    背景（2026-10-11 事故）：Hermes 网关 / telegram-ai-bridge 的代理端口是「启动时读一次」
    的静态配置（.hermes/.env HTTP_PROXY=127.0.0.1:7897 等 5 处硬编码）。代理客户端没在跑时
    端口无监听 → 所有 Telegram 投递失败（用户端表现为「卡住/没回复」），且人不在电脑旁时
    无法远程抢救（Telegram 本身就依赖该代理）。自愈机制 = ~/.local/bin/proxy-watchdog
    （Hermes cron 560502e115f5，每 3 分钟探测→重启 Clash Verge）。
    本检查把「出网总闸」纳入中枢盘点，fail 时随晨报/TG 告警。
    """
    wd = os.path.join(HOME, ".local/bin/proxy-watchdog")
    log = os.path.join(HOME, ".hermes/logs/proxy-watchdog.log")
    recent = ""
    try:
        if os.path.exists(log):
            rec = [l for l in open(log, encoding="utf-8").read().splitlines()
                   if "RECOVERED" in l or "STILL DOWN" in l]
            if rec:
                recent = " · 最近自愈: " + rec[-1].strip()[-58:]
    except OSError:
        pass
    if not os.path.exists(wd):
        return "warn", "proxy-watchdog 脚本缺失（自愈机制未安装）"
    try:
        p = subprocess.run([wd, "--check"], capture_output=True, text=True, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return "error", f"watchdog 无法执行: {str(exc)[:80]}"
    if p.returncode == 0:
        return "ok", "代理端口可用" + recent
    return "fail", ((p.stdout or p.stderr or "").strip()[:80] or "代理端口不可用")


def check_provider_wiring():
    """内置：config 声明的 provider 名是否都解析到 ProviderProfile。

    背景（2026-10-11）：provider 名没有 profile → `chat_completion_helpers` 走 legacy 分支 →
    `build_api_kwargs_extras()` 不被调用 → 按模型的 thinking/推理开关（agent.reasoning_overrides）
    **静默失效**，不报错、日志无痕。典型症状：thinking 模型以「空 content + 只有 reasoning」
    结束回合，用户收到思考 dump 而非回复（日志 `Reasoning-only clean stop`）。
    守卫生效条件：每个 config 声明的 provider 名都要能被 get_provider_profile 解析到。
    """
    py = os.path.join(HOME, "hermes-agent/venv/bin/python3")
    if not os.path.exists(py):
        return "warn", "hermes venv python 缺失，跳过"
    repo = os.path.join(HOME, "hermes-agent")
    cfgp = os.path.join(HOME, ".hermes/config.yaml")
    code = (
        "import sys,json,yaml\n"
        f"sys.path.insert(0,{repo!r})\n"
        "from providers import get_provider_profile as g\n"
        f"cfg=yaml.safe_load(open({cfgp!r},encoding='utf-8'))\n"
        "names=set((cfg.get('providers') or {}).keys())\n"
        "names|={e.get('name') for e in (cfg.get('custom_providers') or [])}\n"
        "names={n for n in names if n and n not in ('api','models','name','default_model')}\n"
        "print(json.dumps(sorted(n for n in names if g(n) is None)))\n"
    )
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return "warn", f"探测异常: {str(exc)[:80]}"
    if p.returncode != 0:
        return "warn", "python 探测失败: " + ((p.stderr or "").strip()[-80:] or f"exit={p.returncode}")
    try:
        bad = json.loads((p.stdout or "[]").strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return "warn", "输出无法解析: " + (p.stdout or "")[-60:]
    if bad:
        return "fail", "无 ProviderProfile 的 provider 名（按模型开关会静默失效）: " + ", ".join(bad[:5])
    return "ok", "provider 名已全部接上 profile"


def check_vault_backup():
    """内置：Vault 备份是否真的在跑、真的成功（2026-10-13 审计 TASK-20261013-001）。

    背景：备份自 2026-10-11 建立起 100% 失败。cron 行原用 /bin/bash 起 rsync 脚本，而 cron 的
    子进程里 /bin/bash、/bin/zsh、/bin/sh 访问 ~/Documents 一律被 TCC 拒绝
    （`Operation not permitted`，rsync 退出码 23）；只有 ~/miniforge3/bin/python3.13 持有
    Full Disk Access。rsync 失败有日志但无人消费 → 页面与晨报长期报绿。

    本检查同时断言两件事，缺一不可：
      接线 —— cron 那一行用的是已授权解释器（改回 shell 即 fail）；
      结果 —— 日志新鲜（>36h 视为未运行）且末行不含 ERROR。
    """
    logf = os.path.join(HOME, ".hermes/logs/vault-backup.log")
    try:
        cr = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:  # noqa: BLE001
        return "warn", f"无法读取 crontab: {str(exc)[:60]}"
    line = next((l.strip() for l in cr.splitlines()
                 if ("backup_vault.py" in l or "backup-vault.sh" in l) and not l.strip().startswith("#")), "")
    if not line:
        return "fail", "找不到 Vault 备份的 crontab 行（备份已从调度里消失）"
    if "/miniforge3/bin/python3" not in line:
        return "fail", f"备份 cron 未用已授权解释器（TCC 会拒绝读 vault）: {line[:64]}"
    if not os.path.exists(logf):
        return "fail", "备份日志不存在（备份从未运行）"
    age_h = (datetime.datetime.now().timestamp() - os.path.getmtime(logf)) / 3600
    try:
        with open(logf, encoding="utf-8", errors="replace") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        last = lines[-1] if lines else ""
    except OSError:
        last = ""
    if "ERROR" in last or "Operation not permitted" in last:
        return "fail", f"最近一次备份失败（{age_h:.1f}h 前）: {last[-66:]}"
    if age_h > 36:
        return "fail", f"备份日志停更 {age_h:.0f}h（>36h 视为未运行）"
    return "ok", f"{age_h:.1f}h 前: {last[-56:]}"


BUILTINS = {"cron": check_cron, "egress": check_egress, "provider_wiring": check_provider_wiring,
            "vault_backup": check_vault_backup}


def run_one(chk, now):
    if chk.get("builtin"):
        st, detail = BUILTINS[chk["builtin"]]()
        rc = 0
    else:
        try:
            p = subprocess.run(chk["cmd"], capture_output=True, text=True, timeout=chk.get("timeout", 300))
            st, detail = parse(chk["name"], (p.stdout or "") + (p.stderr or ""), p.returncode)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            st, detail, rc = "error", f"超时 >{chk.get('timeout')}s", -1
        except Exception as exc:  # noqa: BLE001
            st, detail, rc = "error", str(exc)[:100], -1
    return {"status": st, "detail": detail, "at": now.isoformat(), "rc": rc}


def apply_cap(chk, res):
    """cap: 已知基线问题 → 降级为 warn，避免天天误报（修完断言后移除）。

    降级必须留痕（raw_status + suppressed），否则页面上它等于黑洞：
    读者只看到一个黄色 warn，看不到「原判 fail / 是 critical 检查」（2026-10-13 审计 P0-4）。
    """
    caps = {"warn": ("ok", "warn"), "ok": ("ok",)}
    allowed = caps.get(chk.get("cap"))
    if allowed and res["status"] not in allowed:
        res["raw_status"] = res["status"]
        res["suppressed"] = True
        res["detail"] = f"[已有基线问题] " + res["detail"]
        res["status"] = chk["cap"]
    return res


def main():
    now = datetime.datetime.now()
    st = _load()
    mode = "--all" if "--all" in sys.argv else ("--list" if "--list" in sys.argv else "--due")

    if mode == "--list":
        for c in CHECKS:
            prev = st.get(c["key"], {})
            print(f"  {c['name']:16} {prev.get('status','-'):6} {str(prev.get('at',''))[:19]}  {prev.get('detail','')[:60]}")
        return 0

    ran, failed = [], []
    for c in CHECKS:
        if mode != "--all" and not _due(c, now, st):
            continue
        res = apply_cap(c, run_one(c, now))
        st[c["key"]] = dict(res, name=c["name"])
        ran.append(c["name"])
        if res["status"] in ("fail", "error"):
            failed.append(f"{c['name']}: {res['detail']}")

    _save(st)
    print(f"checks: ran={len(ran)} failed={len(failed)}")
    for f in failed:
        print(f"  🔴 {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
