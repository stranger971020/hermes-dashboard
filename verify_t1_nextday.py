#!/usr/bin/env python3
"""投资看板 · T+1 次日核验（手工诊断工具；原为 2026-11-25 一次性）

⚠️ 2026-11-26 P2：**不要再给它排 cron**。
   结论已经得出（晚间值不是终值：11-24 深市 7/15 只次日被修订），
   核验逻辑已并入 refresh_t1_shares.py 的 08/09 点常设复核（含价格基准与 cache 回写）。
   本脚本仅保留作「怀疑复核链坏了」时的人工对照工具。

目的：确认「当晚 22:30 前抓到的沪深份额」是否就是次日终值，还是次日会被修订。
方法（避免价格噪声，优先比**原始份额**）：
  1. 晚间基线：cache/t1_evening_shares_<D>.json（D 与前一交易日的逐只份额）
     + cache/etf_shares_szse.json[D]（深市 18:30/22:17 快照）
  2. 次日重抓：上交所 D/Dprev、深交所当前快照，逐只比对
  3. 同时用腾讯最新价复算 亿，与页面值对照（09:07 未开盘，最新价=前一交易日收盘）
  4. 推 Telegram 核验结论；若份额有修订 → 用终值覆盖页面并重新发布

用法:
  python3 verify_t1_nextday.py [D]     # 默认 D=quant_data.json 的 date
一次性保护: 写 cache/t1_verify_<D>.done，重复运行直接退出。
"""
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_quant_ratio as C  # noqa: E402
import refresh_t1_shares as R    # noqa: E402

DASH = C.DASHBOARD_PATH
DATA = C.DATA_FILE
LOG_STATE = os.path.join(DASH, "cache")


def send_telegram(text):
    token = None
    try:
        with open(os.path.expanduser("~/.hermes/.env"), "rb") as fh:
            for line in fh:
                if b"TELEGRAM_BOT_TOKEN" in line and not line.startswith(b"#"):
                    token = line.strip().split(b"=", 1)[1].decode()
    except Exception:  # noqa: BLE001
        pass
    if not token:
        print("⚠️ 无 TELEGRAM_BOT_TOKEN，跳过推送")
        return
    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": "8632265776", "text": text}, timeout=20)
    print("推送:", (r.json() or {}).get("ok"))


def main():
    DRY = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    d0 = json.load(open(DATA, encoding="utf-8"))
    D = args[0] if args else d0.get("date")
    marker = os.path.join(LOG_STATE, f"t1_verify_{D}.done")
    if os.path.exists(marker) and not DRY:
        print(f"{D} 已完成次日核验，退出")
        return 0

    hist = os.path.join(C.HISTORY_DIR, f"quant_{D}.json")
    d = json.load(open(hist if os.path.exists(hist) else DATA, encoding="utf-8"))
    if d.get("date") != D:
        print(f"数据日期 {d.get('date')} != {D}，无法核验")
        return 1
    etf = d["etf"]
    entry = next((p for p in etf["netbuy_series"] if p["date"] == D), None)
    if not entry:
        print(f"{D} 不在净申购序列")
        return 1

    base = json.load(open(os.path.join(LOG_STATE, f"t1_evening_shares_{D}.json"),
                          encoding="utf-8"))
    sh_codes = etf.get("sh_broad_codes") or list(base["split_shares"])
    sz_codes = etf.get("sz_broad_codes") or []

    # ── 上交所：原始份额逐只比对 ──
    cur = R.sse_snapshot(D)
    prev = R.sse_snapshot(base["prev"])
    if not cur or not prev:
        print("上交所数据未就绪，核验失败")
        return 1
    sh_diff = [c for c in sh_codes
               if cur.get(c, (None, None))[1] != base["split_shares"].get(c)]
    prev_diff = [c for c in sh_codes
                 if prev.get(c, (None, None))[1] != base["prev_shares"].get(c)]

    # ── 深交所：原始份额逐只比对 ──
    sz_cache = json.load(open(os.path.join(LOG_STATE, "etf_shares_szse.json"),
                              encoding="utf-8"))
    sz_base = sz_cache.get(D) or {}
    full = R.szse_snapshot()
    sz_now = {c: v for c, v in full.items() if c in set(sz_codes)}
    sz_diff = [c for c in sz_codes if sz_now.get(c) != sz_base.get(c)]

    # ── 复算 亿（09:07 未开盘，最新价=前一交易日收盘，与晚间同价基准）──
    px_sh = R.tencent_prices(sh_codes, "sh")
    sh_yi, used_sh = 0.0, 0
    for c in sh_codes:
        if c in cur and c in prev:
            s1, s0, p = cur[c][1], prev[c][1], px_sh.get(c)
            if s1 is not None and s0 is not None and p:
                sh_yi += (s1 - s0) * p
                used_sh += 1
    sh_yi = round(sh_yi / 1e8, 2) if used_sh else None

    sz_yi = R.compute_sz_delta(D, sz_now, sz_cache, sz_codes)

    stored_sh, stored_sz, stored_net = entry.get("sh_yi"), entry.get("sz_yi"), entry.get("net_yi")
    revised = bool(sh_diff or prev_diff or sz_diff)

    # 若份额被修订 → 用终值覆盖页面（dry-run 只报告不写）
    if revised and not DRY:
        if sh_yi is not None:
            entry["sh_yi"] = sh_yi
        if sz_yi is not None:
            entry["sz_yi"] = sz_yi
        entry["complete"] = entry.get("sh_yi") is not None and entry.get("sz_yi") is not None
        entry["net_yi"] = round((entry.get("sh_yi") or 0) + (entry.get("sz_yi") or 0), 2)
        etf["netbuy_today_yi"] = entry["net_yi"]
        etf["netbuy_today_complete"] = entry["complete"]
        etf["netbuy_today_missing"] = [m for m, v in (("沪市", entry.get("sh_yi")),
                                                      ("深市", entry.get("sz_yi"))) if v is None]
        d["signals"] = C.evaluate_signals(etf, d.get("flows") or {})
        d["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        json.dump(d, open(hist, "w", encoding="utf-8"), ensure_ascii=False)
        if d0.get("date") == D:
            json.dump(d, open(DATA, "w", encoding="utf-8"), ensure_ascii=False)
        subprocess.run([sys.executable, os.path.join(DASH, "quant_ratio_render.py"), "quant"],
                       cwd=DASH)
        subprocess.run([sys.executable, os.path.join(DASH, "verify_quant.py")], cwd=DASH)
        subprocess.run(["git", "add", "dashboard_investment.html",
                        f"history/quant_{D}.html", f"history/quant_{D}.json",
                        "quant_data.json"], cwd=DASH, check=False)
        subprocess.run(["git", "commit", "-q", "-m",
                        f"quant: T+1 次日核验修订 {D}（沪 {stored_sh}→{sh_yi} / 深 {stored_sz}→{sz_yi}）"],
                       cwd=DASH, check=False)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=DASH, check=False)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    concl = ("✅ 晚间 22:30 前抓取即为终值（份额零修订）" if not revised
             else "⚠️ 次日发生修订，晚间值非终值")
    msg = (f"🔎 投资看板 T+1 次日核验（D={D}）\n"
           f"晚间基线：沪 {stored_sh}亿 / 深 {stored_sz}亿 / 合计 {stored_net}亿\n"
           f"次日 {now}：沪 {sh_yi}亿 / 深 {sz_yi}亿\n"
           f"份额变化：沪 {len(sh_diff)}/{len(sh_codes)} 只、前一交易日 {len(prev_diff)} 只、"
           f"深 {len(sz_diff)}/{len(sz_codes)} 只\n"
           f"结论：{concl}\n"
           f"https://stranger971020.github.io/hermes-dashboard/dashboard_investment.html")
    print(msg)
    if DRY:
        print("(dry-run：不推送、不写 marker)")
        return 0
    send_telegram(msg)

    json.dump({"date": D, "verified_at": now, "stored": {"sh": stored_sh, "sz": stored_sz},
               "now": {"sh": sh_yi, "sz": sz_yi}, "sh_diff": len(sh_diff),
               "prev_diff": len(prev_diff), "sz_diff": len(sz_diff),
               "revised": revised, "conclusion": concl},
              open(os.path.join(LOG_STATE, f"t1_verify_{D}.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    open(marker, "w").write(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
