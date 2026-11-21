#!/usr/bin/env python3
"""量化持仓（公募·季度·下限）— B 方案

2026-11-22 Claude。口径（必须随结果一起展示）：
    仅公募 · 名称含「量化」 · 季报前十大 · A/C 已按基础名称去重 · 不含私募。
    前十大只覆盖约 50–60% 股票仓位 → 结果是**下限**，不是全部量化持仓。

抓法：天天基金 F10（FundArchivesDatas.aspx?type=jjcc），季度前十大。
分母：Tushare daily_basic 季度末 circ_mv（万元）。
输出：cache/quant_holdings.json；季度刷新，daily 任务用 --if-stale 跳过。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, "/Users/jren/projects/trend-trading-system")
import config  # noqa: E402

DASH = "/Users/jren/projects/hermes-dashboard"
DB = "/Users/jren/projects/trend-trading-system/data_storage/sw_index_data.db"
MAPPING = "/Users/jren/projects/trend-trading-system/data_storage/stock_industry_mapping.csv"
OUT = os.path.join(DASH, "cache", "quant_holdings.json")
H = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
MAX_WORKERS = 1          # 东财 F10 对并发敏感；季度任务宁可慢，不要 301 失败
RETRIES = 5
REQUEST_DELAY = 1.0      # 单线程串行 + 1s 间隔，实测稳定


def _atomic(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".qhold.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _ts_code(code6: str) -> str:
    c = str(code6).zfill(6)
    if c.startswith(("6", "9")):
        return c + ".SH"
    if c.startswith(("0", "2", "3")):
        return c + ".SZ"
    if c.startswith(("4", "8")):
        return c + ".BJ"
    return c + ".SH"


def _quarter_end(period: str) -> str | None:
    m = re.search(r"(\d{4})年(\d)季度", period or "")
    if not m:
        return None
    y, q = int(m.group(1)), int(m.group(2))
    return {1: f"{y}0331", 2: f"{y}0630", 3: f"{y}0930", 4: f"{y}1231"}[q]


def _fetch_one(code: str, year: str) -> tuple[str | None, pd.DataFrame | None]:
    url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={code}&topline=10&year={year}&month=")
    last = None
    for attempt in range(RETRIES):
        try:
            time.sleep(REQUEST_DELAY)
            t = requests.get(url, headers=H, timeout=20).text
            period = re.search(r"(\d{4}年\d季度)", t)
            content = re.search(r'content:"(.*?)",arryear', t, re.S)
            if not content:
                return (period.group(1) if period else None), None
            tables = pd.read_html(io.StringIO(content.group(1)))
            if not tables:
                return (period.group(1) if period else None), None
            df = tables[0].copy()
            df.columns = [str(c).replace("\n", "").replace(" ", "") for c in df.columns]
            code_col = next((c for c in df.columns if "股票代码" in c), None)
            val_col = next((c for c in df.columns if "持仓市值" in c), None)
            if not code_col or not val_col:
                return (period.group(1) if period else None), None
            out = df[[code_col, val_col]].copy()
            out.columns = ["code6", "holding_wan"]
            out["code6"] = (out["code6"].astype(str)
                            .str.replace(r"\.0$", "", regex=True)
                            .str.zfill(6))
            out["holding_wan"] = pd.to_numeric(
                out["holding_wan"].astype(str).str.replace(",", ""), errors="coerce")
            out = out.dropna(subset=["holding_wan"])
            return (period.group(1) if period else None), out
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (attempt + 1))
    return None, None


def _latest_basic(quarter_end: str) -> pd.DataFrame:
    import sqlite3
    import tushare as ts
    c = sqlite3.connect(DB)
    ds = [r[0] for r in c.execute(
        "select distinct trade_date from stock_daily where trade_date<=? "
        "order by trade_date desc limit 8", (quarter_end,))]
    c.close()
    pro = ts.pro_api(config.TUSHARE_TOKEN)
    for d in ds:
        try:
            bs = pro.daily_basic(trade_date=d, fields="ts_code,circ_mv")
            if bs is not None and not bs.empty:
                return bs
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame(columns=["ts_code", "circ_mv"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--if-stale", action="store_true",
                    help="缓存 <80 天则跳过（默认季度刷新）")
    a = ap.parse_args()
    if a.if_stale and os.path.exists(OUT) and not a.force:
        try:
            old = json.load(open(OUT))
            gen = datetime.fromisoformat(old.get("generated_at"))
            cov = old.get("coverage") or {}
            ok = int(cov.get("n_funds_ok") or 0)
            base = int(cov.get("n_funds_base") or 1)
            ratio = ok / base if base else 0
            if (datetime.now() - gen).days < 80 and ratio >= 0.8:
                print(f"⏭ 量化持仓缓存 {old.get('quarter')} 未满 80 天且覆盖 {ok}/{base}，跳过")
                return
            if ratio < 0.8:
                print(f"⚠️ 缓存覆盖不足 {ok}/{base}，强制刷新")
        except Exception:  # noqa: BLE001
            pass

    import akshare as ak
    funds = ak.fund_name_em()
    name_col = "基金简称"
    code_col = "基金代码"
    q = funds[funds[name_col].astype(str).str.contains("量化", na=False)].copy()
    # A/C 份额去重：同一基础名称只取一个代码，避免同一组合重复计数
    q["base"] = (q[name_col].astype(str)
                 .str.replace(r"\(后端\)$", "", regex=True)
                 .str.replace(r"[ABCDEFHIJ]$", "", regex=True))
    q = q.sort_values(code_col).drop_duplicates("base")
    n_listed = int(len(funds[funds[name_col].astype(str).str.contains("量化", na=False)]))
    n_base = len(q)
    enhanced = int(q[name_col].astype(str).str.contains("增强").sum())
    print(f"量化基金: 列表 {n_listed} → 基础名称去重 {n_base}（含量化增强 {enhanced}）")

    year = str(datetime.now().year)
    results, failed = [], 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_fetch_one, str(r[code_col]).zfill(6), year): r for _, r in q.iterrows()}
        for i, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            try:
                period, df = fut.result()
                if df is not None and not df.empty:
                    results.append((period, df))
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                failed += 1
            if i % 50 == 0:
                print(f"[funds] {i}/{n_base} ok={len(results)} failed={failed}", flush=True)

    if not results:
        raise SystemExit("没有取到任何基金持仓")
    periods = [p for p, _ in results if p]
    quarter = max(set(periods), key=periods.count) if periods else "—"
    qend = _quarter_end(quarter) or datetime.now().strftime("%Y%m%d")

    holdings = pd.concat([df for _, df in results], ignore_index=True)
    agg = (holdings.groupby("code6")
           .agg(holding_wan=("holding_wan", "sum"), n_funds=("code6", "size"))
           .reset_index())
    agg["ts_code"] = agg.code6.map(_ts_code)

    mapping = pd.read_csv(MAPPING, dtype=str)
    mapping["code6"] = mapping.ts_code.str[:6]
    basic = _latest_basic(qend)
    agg = agg.merge(basic, on="ts_code", how="left").merge(
        mapping[["code6", "stock_name", "l1_name"]], on="code6", how="left")
    agg["holding_yi"] = agg.holding_wan / 10000.0        # 万元 → 亿元
    agg["circ_yi"] = pd.to_numeric(agg.circ_mv, errors="coerce") / 10000.0
    agg["own_pct"] = agg.holding_yi / agg.circ_yi

    total_holding = float(agg.holding_yi.sum())
    total_circ = float(pd.to_numeric(basic.circ_mv, errors="coerce").sum()) / 10000.0
    market_share = total_holding / total_circ if total_circ else np.nan

    ind = (agg.dropna(subset=["l1_name"])
           .groupby("l1_name")
           .agg(holding_yi=("holding_yi", "sum"), n=("code6", "size"))
           .reset_index())
    ind_circ = (agg.dropna(subset=["l1_name"])
                .groupby("l1_name")["circ_yi"].sum().reset_index())
    ind = ind.merge(ind_circ, on="l1_name", how="left")
    ind["own_pct"] = ind.holding_yi / ind.circ_yi
    ind = ind.sort_values("holding_yi", ascending=False)

    top_value = agg.sort_values("holding_yi", ascending=False).head(10)
    top_own = agg.dropna(subset=["own_pct"]).sort_values("own_pct", ascending=False).head(10)

    payload = {
        "schema": 1,
        "quarter": quarter,
        "quarter_end": qend,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "coverage": {
            "n_funds_listed": n_listed,
            "n_funds_base": n_base,
            "n_funds_ok": len(results),
            "n_funds_failed": failed,
            "n_quant_enhanced": enhanced,
            "n_holdings": int(len(holdings)),
            "n_stocks": int(len(agg)),
            "note": "仅公募·名称含「量化」·季报前十大·A/C 已按基础名称去重·不含私募",
        },
        "market": {
            "total_holding_yi": round(total_holding, 2),
            "total_circ_yi": round(total_circ, 2),
            "share_pct": round(float(market_share) * 100, 3) if market_share == market_share else None,
            "denominator": f"A股流通市值（Tushare circ_mv，{qend}）",
            "caveat": "前十大只覆盖约 50–60% 股票仓位，且不含私募 → 这是公募量化持仓的下限，不是全部量化持仓。",
        },
        "top_stocks_by_value": [
            {"ts_code": r.ts_code, "name": r.stock_name, "industry": r.l1_name,
             "holding_yi": round(float(r.holding_yi), 3),
             "n_funds": int(r.n_funds),
             "own_pct": round(float(r.own_pct) * 100, 3) if pd.notna(r.own_pct) else None}
            for _, r in top_value.iterrows()],
        "top_stocks_by_ownership": [
            {"ts_code": r.ts_code, "name": r.stock_name, "industry": r.l1_name,
             "holding_yi": round(float(r.holding_yi), 3),
             "n_funds": int(r.n_funds),
             "own_pct": round(float(r.own_pct) * 100, 3)}
            for _, r in top_own.iterrows()],
        "top_industries": [
            {"name": r.l1_name, "holding_yi": round(float(r.holding_yi), 2),
             "n": int(r.n), "own_pct": round(float(r.own_pct) * 100, 3)
             if pd.notna(r.own_pct) else None}
            for _, r in ind.head(10).iterrows()],
    }
    _atomic(OUT, payload)
    print(f"✅ {OUT} · {quarter} · 公募量化前十大 {total_holding:.1f}亿 / "
          f"流通 {total_circ:.0f}亿 = {market_share*100:.3f}% · 股票 {len(agg)} · 基金 ok {len(results)}/{n_base}")


if __name__ == "__main__":
    main()
