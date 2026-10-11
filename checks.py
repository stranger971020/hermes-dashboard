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
        return st, f"{passed}/{total} 通过 ({pct}%)，{bad} 项失败"
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


def run_one(chk, now):
    if chk.get("builtin") == "cron":
        st, detail = check_cron()
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
    """cap: 已知基线问题 → 降级为 warn，避免天天误报（修完断言后移除）。"""
    caps = {"warn": ("ok", "warn"), "ok": ("ok",)}
    allowed = caps.get(chk.get("cap"))
    if allowed and res["status"] not in allowed:
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
