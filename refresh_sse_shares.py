#!/usr/bin/env python3
"""投资看板 · 沪市 ETF 份额 T+1 补抓（2026-11-24）

背景：
  · 深交所 ETF 份额是**当日快照**（`fund_etf_scale_szse`，无日期字段，逐日变化实测 10/15 只有更新）。
  · 上交所 ETF 份额是 **T+1**：官方 etf.sse.com.cn 标注「总规模数据每交易日 23:00 更新」，
    Tushare 文档说「交易所次日 8:30 左右更新上一交易日」；实测 2026-11-24 21:32 仍只有 11-23。
  · 因此 18:30 日更只能拿到深市；沪市需在当晚/次日补抓。

行为（幂等）：
  1. quant_data.json 当日条目的 sh_yi 已存在 → 退出 0（零网络）
  2. 抓 D 与前一交易日的沪深份额；任一侧未发布 → 退出 1（等下一轮）
  3. 用「份额变化 × 腾讯最新价」复算沪市净申购（与采集器同口径），合并沪深合计
  4. 重算信号 → 重渲染 → verify → git commit/push（**不发 Telegram**）
每次运行都打一行时间戳，用于事后确定上交所的实际公布时刻。
"""
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_quant_ratio as C  # noqa: E402

DASH = C.DASHBOARD_PATH
DATA = C.DATA_FILE
PY = sys.executable


def sse_snapshot(date_iso):
    """{code: (name, shares)}；未发布/解析失败返回 None。"""
    try:
        df = C.ak.fund_etf_scale_sse(date=date_iso.replace("-", ""))
        df.columns = ["序号", "代码", "简称", "类型", "统计日", "份额"]
        return {str(c): (str(n), C._safe_float(v))
                for c, n, v in zip(df["代码"], df["简称"], df["份额"])}
    except Exception as e:  # noqa: BLE001
        print(f"  SSE {date_iso} 未就绪: {str(e)[:90]}")
        return None


def broad_codes_from_spot():
    """旧数据没有 sh_broad_codes 时，用与采集器一致的现货列表推导（上交所简称
    与新浪名称不同，不能只靠 SSE 简称匹配——实测只有 4/56 命中）。"""
    try:
        s = C.ak.fund_etf_category_sina(symbol="ETF基金")
        codes = s["代码"].astype(str).str.replace(r"^(sh|sz)", "", regex=True)
        names = dict(zip(codes, s["名称"].astype(str)))
        return sorted(c for c, n in names.items()
                      if c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS))
    except Exception as e:  # noqa: BLE001
        print(f"  现货列表推导失败: {str(e)[:80]}")
        return []


def tencent_prices(codes):
    """批量取最新价（腾讯，单次请求、不封 IP）。"""
    import requests
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join("sh" + c for c in batch)
        try:
            r = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            r.encoding = "gbk"
        except Exception as e:  # noqa: BLE001
            print(f"  腾讯行情失败: {str(e)[:80]}")
            continue
        for line in r.text.split(";"):
            if '="' not in line:
                continue
            key = line.split("=")[0].split("_")[-1]      # sh510300
            vals = line.split('"')[1].split("~")
            if len(vals) > 3 and vals[3]:
                out[key[2:]] = C._safe_float(vals[3])
    return out


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(DATA, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:  # noqa: BLE001
        print(f"[{now}] 读取 {DATA} 失败: {e}")
        return 1

    D = d.get("date")
    etf = d.get("etf") or {}
    series = etf.get("netbuy_series") or []
    idx = next((i for i, p in enumerate(series) if p.get("date") == D), None)
    if idx is None:
        print(f"[{now}] D={D} 不在净申购序列中，退出")
        return 1
    entry = series[idx]
    if entry.get("sh_yi") is not None:
        print(f"[{now}] D={D} 沪市已存在（{entry['sh_yi']}亿），无需补抓")
        return 0
    if idx == 0:
        print(f"[{now}] D={D} 无前一交易日，无法算沪市变化")
        return 1
    Dprev = series[idx - 1].get("date")

    cur = sse_snapshot(D)
    if not cur:
        print(f"[{now}] D={D} 沪市份额尚未发布，等待下一轮")
        return 1
    prev = sse_snapshot(Dprev)
    if not prev:
        print(f"[{now}] 前一交易日 {Dprev} 沪市份额缺失，跳过")
        return 1

    codes = etf.get("sh_broad_codes") or broad_codes_from_spot()
    if not codes:   # 最后兜底：SSE 简称匹配（命中率低，仅防完全无数据）
        codes = [c for c, (n, _) in cur.items()
                 if c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS)]
    prices = tencent_prices(codes)

    acc, used = 0.0, 0
    for c in codes:
        if c in cur and c in prev:
            s1, s0, px = cur[c][1], prev[c][1], prices.get(c)
            if s1 is not None and s0 is not None and px:
                acc += (s1 - s0) * px
                used += 1
    if used < max(5, len(codes) // 2):
        print(f"[{now}] 可用价格/份额过少（{used}/{len(codes)}），跳过")
        return 1

    sh = round(acc / 1e8, 2)
    sz = entry.get("sz_yi")
    entry["sh_yi"] = sh
    entry["net_yi"] = round((sh or 0) + (sz or 0), 2) if sz is not None else sh
    entry["complete"] = sz is not None
    etf["netbuy_today_yi"] = entry["net_yi"]
    etf["netbuy_today_complete"] = entry["complete"]
    etf["netbuy_today_missing"] = [m for m, v in (("沪市", entry.get("sh_yi")),
                                                  ("深市", entry.get("sz_yi"))) if v is None]
    d["signals"] = C.evaluate_signals(etf, d.get("flows") or {})
    d["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.makedirs(C.HISTORY_DIR, exist_ok=True)
    with open(os.path.join(C.HISTORY_DIR, f"quant_{D}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)

    print(f"[{now}] ✅ D={D} 沪市补抓成功: sh={sh}亿 sz={sz}亿 合计={entry['net_yi']}亿"
          f"（上交所份额首次可用时刻 ≈ {now}，用于标定公布时间）")

    r = subprocess.run([PY, os.path.join(DASH, "quant_ratio_render.py"), "quant"],
                       cwd=DASH)
    if r.returncode:
        return r.returncode
    r = subprocess.run([PY, os.path.join(DASH, "verify_quant.py")], cwd=DASH)
    if r.returncode:
        return r.returncode

    files = ["dashboard_investment.html", f"history/quant_{D}.html",
             f"history/quant_{D}.json", "quant_data.json"]
    subprocess.run(["git", "add", *files], cwd=DASH, check=False)
    subprocess.run(["git", "commit", "-q", "-m",
                    f"quant: 沪市份额 T+1 补抓并刷新 {D}（sh={sh}亿）"],
                   cwd=DASH, check=False)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=DASH, check=False)
    print(f"[{now}] ✅ 已发布（未推 Telegram）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
