#!/usr/bin/env python3
"""量化活跃度代理 · 日频采集（A 方案）

2026-11-22 Claude。预注册代理（与 backtest/quant_activity_validate.py 同口径）：
    comp = mean( z(turnover_rate), z(range), z(|close/open-1|), z(small_order_share) )
  每日横截面 z → 成交额加权聚合 → micro(最低20% circ_mv) / large(最高20%) / market。

⚠️ 这是「活跃度代理」，不是量化成交占比，也不能识别单笔交易是否为量化。
   验证：2024-01 微盘崩 / 2024-02 量化平仓，事件前 10 日 micro-large spread
   相对前 60 日 z≈+1.0，事件后回落；安慰剂日期不通过。详见
   trend-trading-system/backtest/quant_activity_validation.csv。

输出：cache/quant_activity.json（看板 ⑧ 消费）
缓存：cache/quant_activity_history.json（只补缺失日期，避免每天重拉 60 天）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/jren/projects/trend-trading-system")
import config  # noqa: E402

DASH = "/Users/jren/projects/hermes-dashboard"
DB = "/Users/jren/projects/trend-trading-system/data_storage/sw_index_data.db"
MAPPING = "/Users/jren/projects/trend-trading-system/data_storage/stock_industry_mapping.csv"
CACHE = os.path.join(DASH, "cache", "quant_activity_history.json")
OUT = os.path.join(DASH, "cache", "quant_activity.json")
HISTORY_DAYS = 60
FIELDS_BS = "ts_code,trade_date,turnover_rate,circ_mv,float_share"
FIELDS_MF = ("ts_code,trade_date,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount,"
             "sell_elg_amount,sell_lg_amount,sell_md_amount,sell_sm_amount")


def _atomic(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".qact.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def trade_dates(limit: int = HISTORY_DAYS) -> list[str]:
    c = sqlite3.connect(DB)
    ds = [r[0] for r in c.execute(
        "select distinct trade_date from stock_daily order by trade_date desc limit ?",
        (limit,))]
    c.close()
    return sorted(ds)


def _z(s: pd.Series) -> pd.Series:
    s = s.replace([np.inf, -np.inf], np.nan)
    lo, hi = s.quantile(0.01), s.quantile(0.99)
    s = s.clip(lo, hi)
    mu, sd = s.mean(), s.std()
    return (s - mu) / sd if sd and sd > 0 else s * 0


def _stock_frame(date: str, pro) -> pd.DataFrame:
    c = sqlite3.connect(DB)
    px = pd.read_sql_query(
        "select ts_code,open,high,low,close,pre_close,amount,vol from stock_daily "
        "where trade_date=?", c, params=(date,))
    c.close()
    bs = pro.daily_basic(trade_date=date, fields=FIELDS_BS)
    mf = pro.moneyflow(trade_date=date, fields=FIELDS_MF)
    df = px.merge(bs, on="ts_code", how="inner").merge(mf, on="ts_code", how="inner")
    df = df[(df.amount > 0) & (df.vol > 0) & df.turnover_rate.notna() & df.circ_mv.notna()]
    df["total_order"] = df[["buy_elg_amount", "buy_lg_amount", "buy_md_amount", "buy_sm_amount",
                            "sell_elg_amount", "sell_lg_amount", "sell_md_amount",
                            "sell_sm_amount"]].sum(axis=1)
    df = df[df.total_order > 0].copy()
    df["small_share"] = (df.buy_sm_amount + df.sell_sm_amount) / df.total_order
    df["range"] = (df.high - df.low) / df.pre_close
    df["intraday"] = df.close / df.open - 1
    df["abs_intraday"] = df.intraday.abs()
    df["overnight"] = df.open / df.pre_close - 1
    for col, zc in [("turnover_rate", "z_turn"), ("range", "z_range"),
                    ("abs_intraday", "z_intra"), ("small_share", "z_small")]:
        df[zc] = _z(df[col])
    df["comp"] = df[["z_turn", "z_range", "z_intra", "z_small"]].mean(axis=1)
    df["size_rank"] = df.circ_mv.rank(pct=True)
    df["group"] = np.where(df.size_rank <= 0.20, "micro",
                           np.where(df.size_rank >= 0.80, "large", "mid"))
    return df


def _weighted(g: pd.DataFrame, col: str = "comp") -> float:
    if g.amount.sum() <= 0:
        return float(g[col].mean())
    return float(np.average(g[col], weights=g.amount))


def aggregate(df: pd.DataFrame) -> dict:
    return {
        "market": _weighted(df),
        "micro": _weighted(df[df.group == "micro"]),
        "large": _weighted(df[df.group == "large"]),
        "spread": _weighted(df[df.group == "micro"]) - _weighted(df[df.group == "large"]),
        "n": int(len(df)),
    }


def pct_rank(history: list[dict], key: str, value: float) -> int:
    xs = [h[key] for h in history if h.get(key) is not None]
    if len(xs) < 5:
        return 50
    return int(round(100 * sum(1 for x in xs if x <= value) / len(xs)))


def main():
    from tushare import pro_api
    pro = pro_api(config.TUSHARE_TOKEN)
    dates = trade_dates(HISTORY_DAYS)
    if not dates:
        raise SystemExit("no trade dates")

    hist = []
    if os.path.exists(CACHE):
        try:
            hist = json.load(open(CACHE))
        except Exception:  # noqa: BLE001
            hist = []
    by_date = {h["date"]: h for h in hist if h.get("date")}

    latest_df = None
    for d in dates:
        if d in by_date:
            continue
        try:
            df = _stock_frame(d, pro)
            if df.empty:
                continue
            rec = aggregate(df)
            rec["date"] = d
            by_date[d] = rec
            if d == dates[-1]:
                latest_df = df
            print(f"[quant-activity] {d} n={rec['n']} market={rec['market']:+.2f}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[quant-activity] {d} FAIL {type(e).__name__}: {e}", file=sys.stderr)

    # 只保留最近 HISTORY_DAYS
    hist = [by_date[d] for d in dates if d in by_date]
    _atomic(CACHE, hist)

    asof = dates[-1]
    # 最新日若缓存命中，需要重建 stock frame 以输出 top stocks/industries
    if latest_df is None:
        latest_df = _stock_frame(asof, pro)
    cur = by_date.get(asof) or aggregate(latest_df)

    mapping = pd.read_csv(MAPPING, dtype=str)
    latest_df = latest_df.merge(
        mapping[["ts_code", "stock_name", "l1_name"]], on="ts_code", how="left")
    latest_df["comp_pct"] = latest_df.comp.rank(pct=True) * 100

    ind_rows = []
    for name, g in latest_df.groupby("l1_name"):
        ind_rows.append({
            "l1_name": name,
            "score": _weighted(g),
            "n": len(g),
            "turnover": float(np.average(g.turnover_rate, weights=g.amount)),
        })
    ind = pd.DataFrame(ind_rows).sort_values("score", ascending=False)
    ind["score_pct"] = ind.score.rank(pct=True) * 100

    top_stocks = (latest_df.sort_values("comp_pct", ascending=False)
                  .head(12)[["ts_code", "stock_name", "l1_name", "comp_pct",
                             "turnover_rate", "small_share", "intraday", "range"]]
                  .rename(columns={"stock_name": "name", "l1_name": "industry",
                                   "comp_pct": "score"}))

    payload = {
        "schema": 1,
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "proxy": {
            "name": "量化活跃度代理",
            "formula": "mean( z(换手率), z(振幅), z(|日内涨跌|), z(小单占比) )，每日横截面、成交额加权",
            "caveat": "代理，不是量化成交占比；无法识别单笔交易是否为量化。仅用于活跃度/拥挤度排序。",
        },
        "market": {
            "score": pct_rank(hist, "market", cur["market"]),
            "z": round(cur["market"], 4),
            "micro_score": pct_rank(hist, "micro", cur["micro"]),
            "micro_z": round(cur["micro"], 4),
            "large_score": pct_rank(hist, "large", cur["large"]),
            "large_z": round(cur["large"], 4),
            "spread_score": pct_rank(hist, "spread", cur["spread"]),
            "spread_z": round(cur["spread"], 4),
        },
        "history": [{"date": h["date"], "market": round(h["market"], 4),
                     "micro": round(h["micro"], 4), "large": round(h["large"], 4),
                     "spread": round(h["spread"], 4)} for h in hist],
        "top_industries": [
            {"name": r["l1_name"], "score": round(float(r["score"]), 4),
             "score_pct": round(float(r["score_pct"]), 1), "n": int(r["n"]),
             "turnover": round(float(r["turnover"]), 2)}
            for _, r in ind.head(6).iterrows()],
        "bottom_industries": [
            {"name": r["l1_name"], "score": round(float(r["score"]), 4),
             "score_pct": round(float(r["score_pct"]), 1), "n": int(r["n"]),
             "turnover": round(float(r["turnover"]), 2)}
            for _, r in ind.tail(6).iloc[::-1].iterrows()],
        "top_stocks": [
            {"ts_code": r["ts_code"], "name": r["name"], "industry": r["industry"],
             "score": round(float(r["score"]), 1),
             "turnover_rate": round(float(r["turnover_rate"]), 2),
             "small_share": round(float(r["small_share"]), 3),
             "intraday": round(float(r["intraday"]) * 100, 2),
             "range": round(float(r["range"]) * 100, 2)}
            for _, r in top_stocks.iterrows()],
        "coverage": {"n_stocks": int(len(latest_df)), "n_industries": int(ind.l1_name.nunique()),
                     "history_days": len(hist),
                     "source": "Tushare daily_basic + moneyflow；本地 stock_daily OHLCV"},
    }
    _atomic(OUT, payload)
    print(f"✅ {OUT} · asof {asof} · market {payload['market']['score']} · "
          f"micro {payload['market']['micro_score']} · large {payload['market']['large_score']} · "
          f"spread {payload['market']['spread_score']}")


if __name__ == "__main__":
    main()
