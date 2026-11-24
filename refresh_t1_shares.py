#!/usr/bin/env python3
"""投资看板 · 沪深 ETF 份额 T+1 终刷（2026-11-24）

背景：
  · 深交所份额 = 当日快照（18:30 采集）；但快照可能在晚间继续滚动，18:30 未必是终值。
  · 上交所份额 = T+1（官方 etf.sse.com.cn 标注 23:00 更新；Tushare 称次日 08:30；
    实测 2026-11-24 21:32 仍只有 11-23）。
  · 因此 18:30 只出「深市 + 合计（暂缺沪）」；之后每小时轮询，直到沪市发布。

每小时一次（cron 22:00-次日 09:00）：
  1. 深市：与 18:30 快照逐只对比，记录仍有几只变化；连续 2 次不变 → sz_final，
     之后不再请求深交所（除非沪市发布的终刷那一轮，会再取一次两侧）。
  2. 沪市：轮询 T+1 份额；一旦发布 → 同时抓沪深两侧、复算合计与信号。
  3. 任一侧有新数据 → 重渲染 + verify + **Telegram 推送**（去重：数据日+信号hash）
     + git commit/push。
  4. 沪市已发布且深市已确认 → 标记 done，后续运行零网络退出。
日志 `~/.hermes/logs/sse_shares_refresh.log` 每行记录：
  sz_diff（相对 18:30 仍有变化的只数）/ unchanged（连续未变次数）/ final / 沪市状态，
  可据此判断「18:30 的深市到底是不是终值」。
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_quant_ratio as C  # noqa: E402
import quant_push as P           # noqa: E402

DASH = C.DASHBOARD_PATH
DATA = C.DATA_FILE
SIGNAL_FILE = os.path.join(DASH, "quant_push_signal.txt")
SZ_CACHE = os.path.join(DASH, "cache", "etf_shares_szse.json")
STATE = os.path.join(DASH, "cache", "t1_refresh_state.json")
PY = sys.executable


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def sse_snapshot(date_iso):
    """{code: (name, shares)}；未发布返回 None。"""
    try:
        df = C.ak.fund_etf_scale_sse(date=date_iso.replace("-", ""))
        df.columns = ["序号", "代码", "简称", "类型", "统计日", "份额"]
        return {str(c): (str(n), C._safe_float(v))
                for c, n, v in zip(df["代码"], df["简称"], df["份额"])}
    except Exception:  # noqa: BLE001
        return None


def szse_snapshot():
    df = C.ak.fund_etf_scale_szse()
    df["基金代码"] = df["基金代码"].astype(str)
    return {c: C._safe_float(v) for c, v in zip(df["基金代码"], df["基金份额"])
            if C._safe_float(v) is not None}


def tencent_prices(codes, market="sh"):
    """批量最新价（腾讯，单次请求、不封 IP）。market=sh/sz。"""
    import requests
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join(market + c for c in batch)
        try:
            r = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            r.encoding = "gbk"
        except Exception:  # noqa: BLE001
            continue
        for line in r.text.split(";"):
            if '="' not in line:
                continue
            key = line.split("=")[0].split("_")[-1]
            vals = line.split('"')[1].split("~")
            if len(vals) > 3 and vals[3]:
                out[key[len(market):]] = C._safe_float(vals[3])
    return out


def broad_codes_from_spot():
    """旧数据缺 sh/sz_broad_codes 时，用与采集器一致的现货列表推导。"""
    try:
        s = C.ak.fund_etf_category_sina(symbol="ETF基金")
        codes = s["代码"].astype(str).str.replace(r"^(sh|sz)", "", regex=True)
        names = dict(zip(codes, s["名称"].astype(str)))
        sh = sorted(c for c, n in names.items()
                    if c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS))
        sz = sorted(c for c, n in names.items()
                    if not c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS))
        return sh, sz
    except Exception:  # noqa: BLE001
        return [], []


def compute_sz_delta(D, snap, cache, codes):
    prevs = [x for x in cache if x < D]
    if not prevs or not codes:
        return None
    prev = cache[prevs[-1]]
    prices = tencent_prices(codes, "sz")
    acc, used = 0.0, 0
    for c in codes:
        if c in snap and c in prev:
            s1, s0, px = snap[c], prev[c], prices.get(c)
            if s1 is not None and s0 is not None and px:
                acc += (s1 - s0) * px
                used += 1
    return round(acc / 1e8, 2) if used else None


def compute_sh_delta(D, Dprev, codes):
    cur = sse_snapshot(D)
    if not cur:
        return None, False
    prev = sse_snapshot(Dprev)
    if not prev:
        return None, True
    if not codes:
        codes = [c for c, (n, _) in cur.items()
                 if c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS)]
    prices = tencent_prices(codes, "sh")
    acc, used = 0.0, 0
    for c in codes:
        if c in cur and c in prev:
            s1, s0, px = cur[c][1], prev[c][1], prices.get(c)
            if s1 is not None and s0 is not None and px:
                acc += (s1 - s0) * px
                used += 1
    return (round(acc / 1e8, 2) if used else None), True


def publish(d, D, etf, entry, context):
    """更新合计/信号 → 落盘 → 渲染 → verify → 推送 → git 发布。"""
    sh, sz = entry.get("sh_yi"), entry.get("sz_yi")
    if sh is not None and sz is not None:
        entry["net_yi"] = round(sh + sz, 2)
        entry["complete"] = True
    else:
        entry["net_yi"] = sh if sh is not None else sz
        entry["complete"] = False
    etf["netbuy_today_yi"] = entry["net_yi"]
    etf["netbuy_today_complete"] = entry["complete"]
    etf["netbuy_today_missing"] = [m for m, v in (("沪市", sh), ("深市", sz)) if v is None]
    d["signals"] = C.evaluate_signals(etf, d.get("flows") or {})
    d["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    save_json(DATA, d)
    save_json(os.path.join(C.HISTORY_DIR, f"quant_{D}.json"), d)

    sig = d["signals"]
    with open(SIGNAL_FILE, "w", encoding="utf-8") as fh:
        fh.write(sig["push_line"] if sig["triggered_count"] else "(无触发)")

    import subprocess
    r = subprocess.run([PY, os.path.join(DASH, "quant_ratio_render.py"), "quant"], cwd=DASH)
    if r.returncode:
        return r.returncode
    r = subprocess.run([PY, os.path.join(DASH, "verify_quant.py")], cwd=DASH)
    if r.returncode:
        return r.returncode

    P.push(context)   # 内部按「数据日 + 信号 hash」去重

    files = ["dashboard_investment.html", f"history/quant_{D}.html",
             f"history/quant_{D}.json", "quant_data.json"]
    subprocess.run(["git", "add", *files], cwd=DASH, check=False)
    subprocess.run(["git", "commit", "-q", "-m",
                    f"quant: T+1 份额刷新 {D}（{context}）"], cwd=DASH, check=False)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=DASH, check=False)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✅ 已刷新发布（{context}，未重复推送则静默）")
    return 0


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = load_json(DATA, None)
    if not d:
        print(f"[{now}] 读取 {DATA} 失败")
        return 1
    D = d.get("date")
    etf = d.get("etf") or {}
    series = etf.get("netbuy_series") or []
    idx = next((i for i, p in enumerate(series) if p.get("date") == D), None)
    if idx is None:
        print(f"[{now}] D={D} 不在净申购序列中")
        return 1
    entry = series[idx]
    Dprev = series[idx - 1]["date"] if idx > 0 else None

    all_state = load_json(STATE, {})
    st = all_state.setdefault(D, {"sz_unchanged_polls": 0, "sz_final": False,
                                  "done": False, "last_sz_change": None})
    if st.get("done"):
        print(f"[{now}] D={D} 已完成终刷，零网络退出")
        return 0

    sh_done = entry.get("sh_yi") is not None
    sz_final_before = bool(st.get("sz_final"))

    # 1) 沪市：未完成时先探测是否发布（1 次请求）
    sh_available = sh_done
    sh_new = None
    if not sh_done and Dprev:
        sh_new, sh_available = compute_sh_delta(D, Dprev, etf.get("sh_broad_codes") or [])
        if not sh_available:
            print(f"[{now}] D={D} | 沪市: 未发布 | 深市: final={sz_final_before}")

    # 2) 深市：未确认时逐只对比；沪市发布的那轮强制再取一次两侧
    sz_changed, sz_diff = False, None
    need_sz = (not sz_final_before) or sh_new is not None
    if need_sz:
        cache = load_json(SZ_CACHE, {})
        codes_sz = etf.get("sz_broad_codes") or list((cache.get(D) or {}).keys())
        try:
            full = szse_snapshot()
            snap = {c: v for c, v in full.items() if c in set(codes_sz)}
        except Exception as e:  # noqa: BLE001
            print(f"[{now}] 深市快照失败: {str(e)[:80]}")
            snap = None
        if snap:
            baseline = cache.get(D)
            if baseline is None:
                cache[D] = snap
                save_json(SZ_CACHE, cache)
                baseline = snap
                sz_diff = None
            else:
                keys = set(baseline) | set(snap)
                sz_diff = sum(1 for c in keys if baseline.get(c) != snap.get(c))
                if sz_diff:
                    new_sz = compute_sz_delta(D, snap, cache, codes_sz)
                    if new_sz is not None:
                        cache[D] = snap
                        save_json(SZ_CACHE, cache)
                        entry["sz_yi"] = new_sz
                        sz_changed = True
                        st["sz_unchanged_polls"] = 0
                        st["last_sz_change"] = now
                    else:
                        # 复算失败：不更新基线，下一轮还能检测到变化并重试
                        print(f"[{now}] 深市有变化但复算失败（价格缺失），保留旧值，下轮重试")
                else:
                    st["sz_unchanged_polls"] = int(st.get("sz_unchanged_polls", 0)) + 1
            st["sz_final"] = int(st.get("sz_unchanged_polls", 0)) >= 2

    # 3) 应用沪市新值
    if sh_new is not None:
        entry["sh_yi"] = sh_new
        etf["sh_t1_published"] = True

    etf["sz_final"] = bool(st.get("sz_final"))
    etf["sz_checked_at"] = now

    sz_final_now = bool(st.get("sz_final"))
    sh_done = entry.get("sh_yi") is not None
    final = sh_done and sz_final_now
    if final:
        st["done"] = True
        etf["sz_final"] = True
    save_json(STATE, all_state)

    action = "wait"
    # 深市刚被确认（即使沪市未发）也刷新一次，让页面显示「已确认」；信号未变则推送自动去重
    if (sh_new is not None or sz_changed or (sz_final_now and not sz_final_before)
            or (final and not st.get("_published_final"))):
        context = "T+1 终刷" if final else "T+1 补抓"
        action = "refresh"
        rc = publish(d, D, etf, entry, context)
        if rc:
            return rc
        st["_published_final"] = final
        save_json(STATE, all_state)

    print(f"[{now}] D={D} | 深市: diff={sz_diff} unchanged={st.get('sz_unchanged_polls')} "
          f"final={int(sz_final_now)} | 沪市: {'发布' if sh_done else '未发布'}"
          f"{' sh=' + str(entry.get('sh_yi')) if sh_new is not None else ''} | 动作: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
