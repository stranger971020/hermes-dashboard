#!/usr/bin/env python3
"""盘中小时级资金异动监控 — STANDALONE（验证阶段，未整合进投资看板）

目标：盘中每 30min 抓「主力净流入今日累计」+「宽基ETF成交额(背景)」，
做 delta-based 异动检测（最近一个快照间隔的主力流增量 |Δ| ≥ 阈值 → 触发推 TG）。

为什么用 delta-based 而不是"盘中累计 vs 全天历史分位"：
  盘中日内累计值天然比全天定格值低，直接比近20日全天历史会系统性偏保守、上午几乎不触发。
  delta（最近30min增量）不受此影响、语义清晰（"过去半小时主力资金突然进出 X亿"）、立即可用不冷启动。

口径复用：抓数/重试/降级链全部 import collect_quant_ratio，与主看板同源同口径，不闭门造车重写。

独立产物（不碰 dashboard_investment.html / quant_data.json）：
  - intraday/snapshots.jsonl   逐次快照追加（积累同时段数据，后续可升级"同一时刻历史对比"阈值）
  - ~/.hermes/state/intraday_push_state.json  TG推送去重

R9：外部源失败留痕不静默吞掉；非交易日/无数据如实输出；首次运行只记基线不触发。
"""

import json
import os
import sys
import time
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../intraday
_DASH = os.path.dirname(_HERE)                               # .../hermes-dashboard（collect_quant_ratio 所在）
sys.path.insert(0, "/Users/jren/hermes-agent/.hermes/bin")  # env compat
if _DASH not in sys.path:
    sys.path.insert(0, _DASH)
HERE = _HERE
DASH = _DASH
SNAP_FILE = os.path.join(HERE, "snapshots.jsonl")
PUSH_STATE = os.path.expanduser("~/.hermes/state/intraday_push_state.json")
ENV_FILE = os.path.expanduser("~/.hermes/.env")
CHAT_ID = "8632265776"

# 触发阈值（可调）：最近一个快照间隔的主力净流入增量绝对值 ≥ 该亿元 → 视为异动
DELTA_TRIGGER_YI = float(os.environ.get("INTRADAY_DELTA_TRIGGER", "25"))
# 历史分位窗口（背景展示用，非触发）
HIST_DAYS = 20


def log(msg):
    print(f"[intraday {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _pct_rank(hist, value):
    h = [x for x in hist if x is not None]
    if not h or value is None:
        return None
    return round(sum(1 for x in h if x < value) / len(h) * 100)


def fetch_main_flow():
    """主力净流入：今日累计值 + 近 HIST_DAYS 日历史序列（单位亿）。失败返回 (None, [], err)。"""
    from collect_quant_ratio import _fetch_market_fund_flow, _safe_float
    try:
        df = _fetch_market_fund_flow()
        series = []
        for _, r in df.tail(HIST_DAYS + 1).iterrows():
            v = _safe_float(r.get("主力净流入-净额"))
            if v is not None:
                series.append({"date": str(r["日期"])[:10], "net_yi": round(v / 1e8, 2)})
        if not series:
            return None, [], "klines 解析后为空"
        today = date.today().isoformat()
        # 今日累计值：优先取 date==today；若接口当日行尚未写入则用最后一根（盘中实时累计）
        today_row = next((s for s in series if s["date"] == today), None)
        cum = (today_row or series[-1])["net_yi"]
        hist = [s["net_yi"] for s in series if s["date"] != today]  # 不含今日 → 历史基线
        return round(cum, 2), hist, None
    except Exception as e:  # noqa: BLE001
        return None, [], f"{type(e).__name__}: {str(e)[:140]}"


def fetch_etf_broad_amt():
    """宽基ETF今日累计成交额(背景展示)。降级链：akshare → resilient直连。失败返回 (None, err)。"""
    from collect_quant_ratio import BROAD_ETF_KEYWORDS, _retry, _safe_float, _fetch_etf_spot_resilient
    import akshare as ak

    def _broad(spot):
        names = dict(zip(spot["代码"].astype(str), spot["名称"].astype(str)))
        amt = {c: _safe_float(v) for c, v in zip(spot["代码"].astype(str), spot["成交额"])}
        broad = [c for c, n in names.items() if any(k in n for k in BROAD_ETF_KEYWORDS)]
        return len(broad), round(sum(amt.get(c, 0) or 0 for c in broad) / 1e8, 2)

    try:
        spot = _retry(ak.fund_etf_spot_em, attempts=2)
        cnt, tot = _broad(spot)
        if tot and tot > 0:
            return cnt, tot, None
    except Exception as e1:  # noqa: BLE001
        last = e1
    try:
        spot = _fetch_etf_spot_resilient(page_retries=4)
        cnt, tot = _broad(spot)
        if tot and tot > 0:
            return cnt, tot, None
    except Exception as e2:  # noqa: BLE001
        last = f"{type(e2).__name__}: {str(e2)[:120]}"
    return None, None, str(last)


def load_last_snapshot(today_iso):
    """读今日最近一条快照（用于 delta）。"""
    if not os.path.exists(SNAP_FILE):
        return None
    last = None
    with open(SNAP_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("date") == today_iso and rec.get("main_flow_cum_yi") is not None:
                last = rec
    return last


def append_snapshot(rec):
    os.makedirs(HERE, exist_ok=True)
    with open(SNAP_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def push_telegram(text):
    """推 TG，当天同方向+同档位去重。返回 (pushed: bool, reason: str)。"""
    import hashlib
    import requests

    today = date.today().isoformat()
    # 去重键：方向 + 触发值（四舍五入到亿）——同一异动档当天只推一次
    key = hashlib.sha1(text.encode()).hexdigest()[:12]
    state = {}
    if os.path.exists(PUSH_STATE):
        try:
            state = json.load(open(PUSH_STATE))
        except Exception:  # noqa: BLE001
            state = {}
    if state.get("date") == today and state.get("sig_hash") == key:
        return False, "同一异动今日已推送，跳过（防刷屏）"

    token = None
    with open(ENV_FILE, "rb") as f:
        for line in f:
            if b"TELEGRAM_BOT_TOKEN" in line and not line.startswith(b"#"):
                token = line.strip().split(b"=", 1)[1].decode("utf-8")
    if not token:
        return False, "未找到 TELEGRAM_BOT_TOKEN，跳过推送"

    msg = ("📈 盘中资金异动（小时级监控·standalone）\n" + text)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg}, timeout=20)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    if body.get("ok"):
        os.makedirs(os.path.dirname(PUSH_STATE), exist_ok=True)
        json.dump({"date": today, "sig_hash": key,
                   "message_id": body["result"]["message_id"]}, open(PUSH_STATE, "w"))
        return True, f"已推送 (message_id={body['result']['message_id']})"
    return False, f"推送失败: {r.text[:120]}"


def main():
    t0 = time.time()
    today_iso = date.today().isoformat()

    # 交易日判断（非交易日静默跳过）
    from collect_quant_ratio import get_trading_dates
    _, is_trading = get_trading_dates(today_iso)
    if not is_trading:
        log(f"⏸ {today_iso} 非交易日，跳过")
        return

    # 1) 主力净流入（核心）
    cum, hist, mf_err = fetch_main_flow()
    if cum is None:
        log(f"❌ 主力资金抓取失败：{mf_err}")
        append_snapshot({"date": today_iso, "time": datetime.now().strftime("%H:%M"),
                         "main_flow_cum_yi": None, "etf_amt_yi": None,
                         "delta_yi": None, "triggered": False, "error": mf_err})
        return

    # 2) ETF成交额（背景，失败不阻断）
    etf_cnt, etf_amt, etf_err = fetch_etf_broad_amt()

    # 3) delta-based 异动检测
    last = load_last_snapshot(today_iso)
    delta = None
    if last:
        prev_cum = last.get("main_flow_cum_yi")
        if prev_cum is not None:
            delta = round(cum - prev_cum, 2)

    triggered = delta is not None and abs(delta) >= DELTA_TRIGGER_YI
    direction = "净流入突增" if (triggered and delta > 0) else ("净流出突增" if (triggered and delta < 0) else "")

    # 4) 历史分位（背景展示，标注盘中口径）
    pct = _pct_rank(hist, cum)

    rec = {
        "date": today_iso,
        "time": datetime.now().strftime("%H:%M"),
        "main_flow_cum_yi": cum,
        "etf_amt_yi": etf_amt,
        "delta_yi": delta,
        "pct_rank_of_hist": pct,
        "triggered": triggered,
        "direction": direction,
    }
    append_snapshot(rec)

    log(f"主力净流入今日累计 {cum}亿 | 最近间隔增量 Δ={delta if delta is not None else '首次(基线)'}亿 | "
        f"ETF宽基成交 {etf_amt if etf_amt is not None else '缺失(' + (etf_err or '')[:30] + ')'}亿 | "
        f"历史分位 P{pct}（盘中口径）| 触发={triggered}")

    # 5) 推送
    if triggered:
        etf_line = f"\n宽基ETF今日成交 {etf_amt}亿" if etf_amt is not None else ""
        text = (f"{direction}：最近间隔主力净流入变化 Δ{delta:+.1f}亿\n"
                f"今日累计主力净流入 {cum:+.1f}亿（近{len(hist)}日分位 P{pct}，盘中口径）\n"
                f"{etf_line}\n阈值 |Δ|≥{DELTA_TRIGGER_YI:.0f}亿 · standalone 验证中")
        pushed, reason = push_telegram(text)
        log(f"📢 {reason}")
    elif delta is None:
        log("⏭ 首次运行，仅记录基线（无上次快照可比），不触发")
    else:
        log(f"⏭ |Δ|={abs(delta):.1f}亿 < 阈值{DELTA_TRIGGER_YI:.0f}亿，未触发，静默")

    log(f"完成，耗时 {int(time.time() - t0)}s → snapshot 已落盘 {SNAP_FILE}")


if __name__ == "__main__":
    main()
