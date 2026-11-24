#!/usr/bin/env python3
"""投资看板 · 主力资金晚间补抓（2026-11-24）

背景：工作日 18:30 的日更只抓一次；东财 push2his 会因 IP 级风控整 host
空响应（2026-11-24 18:31 实测 daykline/kline/ETF 同时 RemoteDisconnected），
而当天晚些时候源往往已恢复。单次失败后不再原地重试（会加重风控），
改为晚间 21/22/23 点各补一次。

行为（幂等）：
  1. quant_data.json 的 date != 今天 → 退出 0（不补历史）
  2. 今日 main_flow_series 已存在 → 退出 0（零网络）
  3. 否则单发抓取（daykline → kline 各 1 次），合并 flows + 重算 signals
  4. 重渲染 + verify 通过后 git commit/push；**不发 Telegram**（信号推送仍归主 job）
失败如实 exit 1，不伪造数值。
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_quant_ratio as C  # noqa: E402  (同目录模块)

DASH = C.DASHBOARD_PATH
DATA = C.DATA_FILE
PY = sys.executable


def main():
    today = date.today().isoformat()
    try:
        with open(DATA, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:  # noqa: BLE001
        print(f"读取 {DATA} 失败: {e}")
        return 1

    if d.get("date") != today:
        print(f"skip: 数据日期 {d.get('date')} != 今天 {today}（非交易日或未日更）")
        return 0

    series = (d.get("flows") or {}).get("main_flow_series") or []
    if series and series[-1].get("date") == today:
        print("skip: 今日主力序列已存在")
        return 0

    flows = C.collect_flows(include_northbound=False)
    if not flows.get("main_flow_series"):
        print(f"fail: 补抓仍失败（{str(flows.get('main_flow_error'))[:140]}）")
        return 1

    d["flows"] = flows
    d["signals"] = C.evaluate_signals(d.get("etf") or {}, flows)
    d["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.makedirs(C.HISTORY_DIR, exist_ok=True)
    with open(os.path.join(C.HISTORY_DIR, f"quant_{today}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    print(f"✅ 已补抓今日主力资金: {flows['main_flow_series'][-1]}")

    r = subprocess.run([PY, os.path.join(DASH, "quant_ratio_render.py"), "quant"],
                       cwd=DASH)
    if r.returncode:
        return r.returncode
    r = subprocess.run([PY, os.path.join(DASH, "verify_quant.py")], cwd=DASH)
    if r.returncode:
        return r.returncode

    files = ["dashboard_investment.html", f"history/quant_{today}.html",
             f"history/quant_{today}.json", "quant_data.json"]
    subprocess.run(["git", "add", *files], cwd=DASH, check=False)
    subprocess.run(["git", "commit", "-q", "-m",
                    f"quant backfill: 主力资金晚间补抓 {today}"],
                   cwd=DASH, check=False)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=DASH, check=False)
    print("✅ 已发布（未推 Telegram）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
