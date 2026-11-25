#!/usr/bin/env python3
"""盘中小时级资金监控 — STANDALONE（心跳模式，2026-11-25 起）

调度：cron `0 10,11,14,15 * * 1-5`（交易日 10:00/11:00/14:00/15:00，每整点一次）。
每次运行抓「主力净流入今日累计」+「宽基ETF成交额(背景)」：

- **心跳推送**：每个时段推一行状态（累计 / 较上次Δ / 历史分位），同一钟点去重。
- **异动标记**：最近间隔 |Δ| ≥ DELTA_TRIGGER_YI → 消息加 🚨 前缀与方向行（不再单独双推）。
- **故障可见**：抓取连续失败 ≥3 次 → 推一条告警（当天只推一次），恢复后心跳里注明。

为什么 delta-based：盘中日内累计值天然比全天定格值低，直接比近20日全天历史会系统性
偏保守；delta（距上次快照的增量）语义清晰、不冷启动。阈值含义随间隔变化（现=每小时）。

口径复用：抓数/重试/降级链全部 import collect_quant_ratio，与主看板同源同口径。
独立产物（不碰 dashboard_investment.html / quant_data.json）：
  - intraday/snapshots.jsonl   逐次快照追加
  - ~/.hermes/state/intraday_push_state.json  推送去重
  - ~/.hermes/state/intraday_fail_state.json  连续失败计数（告警用）

R9：外部源失败留痕不静默吞掉；非交易日/无数据如实输出；首次运行只记基线。
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
SNAP_FILE = os.path.join(_HERE, "snapshots.jsonl")
PUSH_STATE = os.path.expanduser("~/.hermes/state/intraday_push_state.json")
FAIL_STATE = os.path.expanduser("~/.hermes/state/intraday_fail_state.json")
ENV_FILE = os.path.expanduser("~/.hermes/.env")
CHAT_ID = "8632265776"

# 异动阈值（可调）：距上次快照的主力净流入增量绝对值 ≥ 该亿元 → 🚨 标记。
# 注意含义随调度间隔变化：当前整点调度下 = 「最近一小时」增量。
DELTA_TRIGGER_YI = float(os.environ.get("INTRADAY_DELTA_TRIGGER", "25"))
HIST_DAYS = 20
ALERT_AT_FAILS = 3   # 连续失败 ≥N 次 → 推一条告警（当天只推一次）


def log(msg):
    print(f"[intraday {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {} if default is None else default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def _pct_rank(hist, value):
    h = [x for x in hist if x is not None]
    if not h or value is None:
        return None
    return round(sum(1 for x in h if x < value) / len(h) * 100)


def fetch_main_flow():
    """主力净流入：今日累计值 + 近 HIST_DAYS 日历史序列（单位亿）。

    返回 (cum, hist, err, stale)。stale=True = daykline 尚无今日行，cum 取上一交易日
    （东财 edge node 数据不同步时会出现；此时 delta 不可信、不触发异动）。
    """
    from collect_quant_ratio import _fetch_market_fund_flow, _safe_float
    try:
        df = _fetch_market_fund_flow()
        series = []
        for _, r in df.tail(HIST_DAYS + 1).iterrows():
            v = _safe_float(r.get("主力净流入-净额"))
            if v is not None:
                series.append({"date": str(r["日期"])[:10], "net_yi": round(v / 1e8, 2)})
        if not series:
            return None, [], "klines 解析后为空", False
        today = date.today().isoformat()
        # 今日累计值：优先取 date==today；若接口当日行尚未写入则用最后一根（盘中实时累计）
        today_row = next((s for s in series if s["date"] == today), None)
        cum = (today_row or series[-1])["net_yi"]
        hist = [s["net_yi"] for s in series if s["date"] != today]  # 不含今日 → 历史基线
        return round(cum, 2), hist, None, (today_row is None)
    except Exception as e:  # noqa: BLE001
        return None, [], f"{type(e).__name__}: {str(e)[:140]}", False


def fetch_etf_broad_amt():
    """宽基ETF今日累计成交额(背景展示)。降级链：akshare → resilient直连 → 新浪源。失败返回 (None, err)。"""
    from collect_quant_ratio import BROAD_ETF_KEYWORDS, _retry, _safe_float, _fetch_etf_spot_resilient
    import akshare as ak
    import pandas as pd

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
    try:  # 第三级兜底：新浪源（东财整体限流时仍可用，实测 ~1s、量级合理）
        s = _retry(ak.fund_etf_category_sina, symbol="ETF基金", attempts=3)
        code = s["代码"].astype(str).str.replace(r"^(sh|sz)", "", regex=True)
        spot = pd.DataFrame({"代码": code, "名称": s["名称"], "成交额": s["成交额"]})
        cnt, tot = _broad(spot)
        if tot and tot > 0:
            return cnt, tot, None
    except Exception as e3:  # noqa: BLE001
        last = f"{type(e3).__name__}: {str(e3)[:120]}"
    return None, None, str(last)


def load_last_snapshot(today_iso):
    """读今日最近一条可信快照（用于 delta）：cum 非空且非 stale 降级值。"""
    if not os.path.exists(SNAP_FILE):
        return None
    last = None
    with open(SNAP_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if (rec.get("date") == today_iso and rec.get("main_flow_cum_yi") is not None
                    and not rec.get("stale")):
                last = rec
    return last


def append_snapshot(rec):
    os.makedirs(_HERE, exist_ok=True)
    with open(SNAP_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def push_telegram(text, dedup_key):
    """推 TG，按 dedup_key 去重（心跳=当天同钟点；告警=当天一次）。返回 (pushed: bool, reason: str)。"""
    import requests

    state = _load_json(PUSH_STATE, {})
    if state.get("key") == dedup_key and state.get("date") == date.today().isoformat():
        return False, "同一键今日已推送，跳过（防刷屏）"

    token = None
    with open(ENV_FILE, "rb") as f:
        for line in f:
            if b"TELEGRAM_BOT_TOKEN" in line and not line.startswith(b"#"):
                token = line.strip().split(b"=", 1)[1].decode("utf-8")
    if not token:
        return False, "未找到 TELEGRAM_BOT_TOKEN，跳过推送"

    msg = ("📈 盘中主力资金监控（standalone）\n" + text)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg}, timeout=20)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    if body.get("ok"):
        _save_json(PUSH_STATE, {"date": date.today().isoformat(), "key": dedup_key,
                                "message_id": body["result"]["message_id"]})
        return True, f"已推送 (message_id={body['result']['message_id']})"
    return False, f"推送失败: {r.text[:120]}"


def record_failure(err_msg):
    """累计当天连续失败；≥ALERT_AT_FAILS 且未告警过 → 推一条。返回 (count, pushed_alert, reason)。"""
    today = date.today().isoformat()
    now_hm = datetime.now().strftime("%H:%M")
    st = _load_json(FAIL_STATE, {})
    if st.get("date") != today:
        st = {"date": today, "count": 0, "first_fail": None}
    st["count"] += 1
    st.setdefault("first_fail", now_hm)

    pushed_alert, reason = False, ""
    if st["count"] >= ALERT_AT_FAILS and not st.get("alerted"):
        text = (f"⚠️ 连续 {st['count']} 次抓取失败\n"
                f"今日首次失败 {st['first_fail']} ｜ 最近 {now_hm}\n"
                f"{str(err_msg)[:100]}\n每整点自动重试，恢复后心跳中注明")
        pushed_alert, reason = push_telegram(text, dedup_key=f"fail-{today}")
        if pushed_alert:
            st["alerted"] = True
    _save_json(FAIL_STATE, st)
    return st["count"], pushed_alert, reason


def clear_failures():
    """成功一轮 → 清零。返回当天此前累计的失败次数（用于心跳里的恢复提示）。"""
    today = date.today().isoformat()
    st = _load_json(FAIL_STATE, {})
    prev = st.get("count", 0) if st.get("date") == today else 0
    _save_json(FAIL_STATE, {"date": today, "count": 0, "first_fail": None})
    return prev


def main():
    t0 = time.time()
    today_iso = date.today().isoformat()

    # 交易日判断（非交易日静默跳过）
    from collect_quant_ratio import get_trading_dates
    _, is_trading = get_trading_dates(today_iso)
    if not is_trading:
        log(f"⏸ {today_iso} 非交易日，跳过")
        return

    # 1) 主力净流入（核心）— 失败走「故障可见」路径
    cum, hist, mf_err, stale = fetch_main_flow()
    if cum is None:
        count, pushed_alert, reason = record_failure(mf_err)
        log(f"❌ 主力资金抓取失败：{mf_err}")
        log(f"⏭ 今日连续失败 {count} 次（≥{ALERT_AT_FAILS} 告警）| {'📢 ' + reason if pushed_alert else '未达告警阈值或今日已告警'}")
        append_snapshot({"date": today_iso, "time": datetime.now().strftime("%H:%M"),
                         "main_flow_cum_yi": None, "etf_amt_yi": None,
                         "delta_yi": None, "triggered": False, "error": mf_err})
        return

    # 2) ETF成交额（背景，失败不阻断）
    etf_cnt, etf_amt, etf_err = fetch_etf_broad_amt()

    # 3) delta-based 异动检测（标记用，不再单独双推）
    last = load_last_snapshot(today_iso)
    delta = None
    if last:
        prev_cum = last.get("main_flow_cum_yi")
        if prev_cum is not None:
            delta = round(cum - prev_cum, 2)

    triggered = (not stale) and delta is not None and abs(delta) >= DELTA_TRIGGER_YI
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
        "stale": stale,
    }
    append_snapshot(rec)

    # 5) 心跳推送（每次运行必推，同钟点去重）
    prev_fails = clear_failures()
    head = f"🚨 {direction}" if triggered else f"{datetime.now().strftime('%H:%M')} 心跳"
    lines = [head]
    if stale:
        # daykline 尚无今日行 → cum 是上一交易日值，Δ 不可信（不触发、只标注）
        lines.append(f"主力累计 {cum:+.1f}亿 ⚠️ 接口暂无今日行（按最近交易日）")
    else:
        delta_txt = "基线" if delta is None else f"{delta:+.1f}"
        lines.append(f"主力累计 {cum:+.1f}亿 ｜ Δ较上次 {delta_txt}")
    if triggered:
        lines.append(f"|Δ|≥{DELTA_TRIGGER_YI:.0f}亿 异动阈值 · 近{len(hist)}日分位 P{pct}")
    else:
        ptxt = f"P{pct}" if pct is not None else "P?"
        lines.append(f"历史分位 {ptxt}（盘中口径）")
    if etf_amt is not None:
        lines.append(f"宽基ETF今日成交 {etf_amt}亿")
    if prev_fails >= ALERT_AT_FAILS:
        lines.append(f"✅ 此前连续失败{prev_fails}次，现已恢复")
    text = "\n".join(lines)

    pushed, reason = push_telegram(text, dedup_key=f"hb-{today_iso}-{datetime.now().strftime('%H')}")
    log(f"主力净流入今日累计 {cum}亿 | Δ={delta if delta is not None else '首次(基线)'}亿 | "
        f"ETF宽基成交 {etf_amt if etf_amt is not None else '缺失(' + (etf_err or '')[:30] + ')'}亿 | "
        f"历史分位 P{pct}（盘中口径）| 触发={triggered}")
    log(f"📢 {reason}")
    log(f"完成，耗时 {int(time.time() - t0)}s → snapshot 已落盘 {SNAP_FILE}")


if __name__ == "__main__":
    main()
