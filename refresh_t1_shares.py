#!/usr/bin/env python3
"""投资看板 · 沪深 ETF 份额 T+1 终刷（2026-11-24，2026-11-26 加次日早间复核）

背景：
  · 深交所份额 = 当日快照（18:30 采集）；但快照可能在晚间继续滚动，18:30 未必是终值。
  · 上交所份额 = T+1（官方 etf.sse.com.cn 标注 23:00 更新；Tushare 称次日 08:30；
    实测 2026-11-24 21:32 仍只有 11-23）。
  · 因此 18:30 只出「深市 + 合计（暂缺沪）」；之后每小时轮询，直到沪市发布。

每小时一次（cron 22:00-次日 09:00）：
  1. 深市：与 18:30 快照逐只对比，记录仍有几只变化；连续 2 次不变 → sz_final。
  2. 沪市：轮询 T+1 份额；一旦发布 → 同时抓沪深两侧、复算合计与信号。
  3. 任一侧有新数据 → 重渲染 + verify + **Telegram 推送**（去重：数据日+信号hash）
     + git commit/push。

2026-11-26 修正「01:17 就 done、08-09 点修订窗口被漏掉」：
  · 实测证据（verify_t1_nextday.py，11-24 → 11-25 09:07）：晚间深市 2.69亿，
    次日上午复核 7/15 只份额被修订，复算 -12.62亿。**晚间「连续 2 次不变」不等于终值**。
  · 因此 done 只在 09 点早间复核之后置位；02-07 点静默（不请求），08/09 点各复核一次：
      - 深市：当前快照逐只 vs cache[D]，有修订 → 用「晚间价格基准」复算并更新 cache[D]
        （更新 cache 是关键：次日 18:30 collect 会用它重算历史，否则修订会被覆盖回旧值）。
      - 沪市：上交所按日期可回溯，与晚间存的逐只快照比对（实测 0/56 修订）。
      - 主力资金：08 点重抓一次，仅当 D 日数值变化时更新（用户实测次日 8-9 点才定稿）。
  · 价格基准落盘到 cache/etf_price_basis.json：09:17 已过集合竞价，不能用实时价复算。
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
SH_CACHE = os.path.join(DASH, "cache", "etf_shares_sse.json")
PRICE_BASIS = os.path.join(DASH, "cache", "etf_price_basis.json")
STATE = os.path.join(DASH, "cache", "t1_refresh_state.json")
PY = sys.executable
# 早间复核窗口（cron 08:17 / 09:17）。09 点这轮通过后才置 done。
MORNING_HOURS = (8, 9)


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


def delta_from_snap(cur, prev, codes, prices):
    """逐只 (cur - prev) × price 求和（亿元）。缺份额/价格的不计入。"""
    acc, used = 0.0, 0
    for c in codes:
        s1, s0, px = cur.get(c), prev.get(c), prices.get(c)
        if s1 is not None and s0 is not None and px:
            acc += (s1 - s0) * px
            used += 1
    return round(acc / 1e8, 2) if used else None


def compute_sz_delta(D, snap, cache, codes, prices=None):
    """深市当日净申购。prices 可传入「晚间价格基准」，早间复核时不能用实时价。"""
    prevs = [x for x in cache if x < D]
    if not prevs or not codes:
        return None
    prev = cache[prevs[-1]]
    prices = prices if prices is not None else tencent_prices(codes, "sz")
    return delta_from_snap(snap, prev, codes, prices)


def compute_sh_delta(D, Dprev, codes, prices=None):
    cur = sse_snapshot(D)
    if not cur:
        return None, False
    prev = sse_snapshot(Dprev)
    if not prev:
        return None, True
    if not codes:
        codes = [c for c, (n, _) in cur.items()
                 if c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS)]
    prices = prices if prices is not None else tencent_prices(codes, "sh")
    cur_s = {c: v[1] for c, v in cur.items()}
    prev_s = {c: v[1] for c, v in prev.items()}
    return delta_from_snap(cur_s, prev_s, codes, prices), True


# ── 次日复核辅助：价格基准 / 上交所逐只快照 ─────────────────────────
def save_price_basis(D, sz=None, sh=None):
    """落盘晚间复算所用的价格。09:17 已过集合竞价，早间复核必须复用同一基准价，
    否则「份额修订」和「价格漂移」会混在一起，算出一个既非晚间也非终值的数。"""
    allb = load_json(PRICE_BASIS, {})
    e = allb.get(D) or {}
    if sz:
        e["sz"] = sz
    if sh:
        e["sh"] = sh
    allb[D] = e
    for k in sorted(allb)[:-30]:
        allb.pop(k, None)
    save_json(PRICE_BASIS, allb)


def save_sh_snapshot(D, shares):
    allc = load_json(SH_CACHE, {})
    allc[D] = shares
    for k in sorted(allc)[:-30]:
        allc.pop(k, None)
    save_json(SH_CACHE, allc)


def morning_recheck(d, D, etf, entry, st, now):
    """08/09 点早间复核：交易所可能在次日开盘前修订上一交易日份额。

    返回 (changed, verified)：
      changed  = 有修订并已更新 entry（调用方负责 publish）
      verified = 深市快照确实取到并比对过。取不到 → False，调用方不应置 done
                 （宁可下一交易日重来，也不要把「没验成」记成「验过了」）。
    安全闸：若变化只数超过一半，判为「快照已切到新交易日」，不当作修订。
    """
    changed = False
    verified = True
    basis = (load_json(PRICE_BASIS, {}) or {}).get(D) or {}
    cache = load_json(SZ_CACHE, {})
    codes_sz = etf.get("sz_broad_codes") or list((cache.get(D) or {}).keys())
    base = cache.get(D)

    if base and codes_sz:
        try:
            full = szse_snapshot()
            snap = {c: v for c, v in full.items() if c in set(codes_sz)}
        except Exception as e:  # noqa: BLE001
            print(f"[{now:%H:%M}] 早间深市复核抓取失败: {str(e)[:80]}")
            snap = None
            verified = False
        if snap:
            diff = sum(1 for c in set(base) | set(snap) if base.get(c) != snap.get(c))
            if diff > max(3, len(codes_sz) // 2):
                print(f"[早间复核] 深市变化 {diff}/{len(codes_sz)} 只 → "
                      f"疑似快照已切新交易日，不按修订处理")
            elif diff:
                px = basis.get("sz")
                if not px:
                    print("[早间复核] 深市有修订但缺晚间价格基准，无法复算（保留旧值）")
                else:
                    # 先更新 cache[D] 再复算：次日 18:30 collect 会按 cache 重算历史，
                    # 不更新缓存的话修订会在下一次 collect 时被覆盖回旧值（11-24 实测）。
                    cache[D] = snap
                    save_json(SZ_CACHE, cache)
                    new_sz = compute_sz_delta(D, snap, cache, codes_sz, prices=px)
                    if new_sz is not None:
                        entry["sz_yi"] = new_sz
                        # 不动 sz_snapshot_at（那是数据实际到达时刻）；单独记修订时刻
                        etf["sz_revised_at"] = f"{now:%m-%d %H:%M}"
                        etf["sz_morning_revision"] = True
                        changed = True
                        print(f"[早间复核] 深市修订 {diff}/{len(codes_sz)} 只 → sz={new_sz}亿")
            else:
                print(f"[早间复核] 深市份额无修订（0/{len(codes_sz)}）")

    sh_base = (load_json(SH_CACHE, {}) or {}).get(D)
    codes_sh = etf.get("sh_broad_codes") or []
    if sh_base and codes_sh:
        cur = sse_snapshot(D)
        if cur:
            cur_s = {c: v[1] for c, v in cur.items()}
            diff = sum(1 for c in codes_sh if cur_s.get(c) != sh_base.get(c))
            if 0 < diff <= max(3, len(codes_sh) // 2):
                px = basis.get("sh")
                if px:
                    new_sh = delta_from_snap(cur_s, sh_base, codes_sh, px)
                    if new_sh is not None:
                        entry["sh_yi"] = new_sh
                        etf["sh_revised_at"] = f"{now:%m-%d %H:%M}"
                        allc = load_json(SH_CACHE, {})
                        allc[D] = cur_s
                        save_json(SH_CACHE, allc)
                        changed = True
                        print(f"[早间复核] 沪市修订 {diff}/{len(codes_sh)} 只 → sh={new_sh}亿")
    return changed, verified


def morning_recheck_flows(d, D, st, now):
    """08/09 点重抓主力资金：用户实测次日 8-9 点才定稿。
    只回填 D 当日一条（不动今天的盘中序列）；抓取失败不标 verified，下一小时再试。"""
    if st.get("flows_morning_verified"):
        return False
    try:
        # save_cache=False：早间数据只用于比对，不能污染「当日已抓」缓存
        flows = C.collect_flows(include_northbound=False, save_cache=False)
    except Exception as e:  # noqa: BLE001
        print(f"[{now:%H:%M}] 早间主力资金复核抓取失败: {str(e)[:80]}")
        return False
    series = flows.get("main_flow_series") or []
    new = next((x for x in series if x.get("date") == D), None)
    if new is None:
        print("[早间复核] 主力资金源未返回 D 日数据，下轮再试")
        return False
    st["flows_morning_verified"] = True
    existing = d.get("flows") or {}
    rows = existing.get("main_flow_series") or []
    old = next((x for x in rows if x.get("date") == D), None)
    if old is None or new.get("net_yi") is None:
        return False
    if abs((new.get("net_yi") or 0) - (old.get("net_yi") or 0)) < 0.01:
        print(f"[早间复核] 主力资金无修订（{old.get('net_yi')}亿）")
        return False
    for i, x in enumerate(rows):
        if x.get("date") == D:
            rows[i] = new
            break
    existing["main_flow_series"] = rows
    d["flows"] = existing
    print(f"[早间复核] 主力资金修订: {old.get('net_yi')} → {new.get('net_yi')}亿")
    return True


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
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
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
    st.setdefault("morning_verified", False)
    st.setdefault("flows_morning_verified", False)
    if st.get("done"):
        print(f"[{now}] D={D} 已完成终刷与早间复核，零网络退出")
        return 0

    hour = now_dt.hour
    is_morning = hour in MORNING_HOURS
    sh_done = entry.get("sh_yi") is not None
    sz_final_before = bool(st.get("sz_final"))

    # 0) 早间复核窗口（08:17 / 09:17）：交易所可能修订上一交易日份额；
    #    主力资金用户实测次日 8-9 点才定稿。09 点这轮通过后才置 done。
    if is_morning and not st.get("morning_verified"):
        if sh_done and sz_final_before:
            etf_changed, verified = morning_recheck(d, D, etf, entry, st, now_dt)
            flows_changed = morning_recheck_flows(d, D, st, now_dt)
            if etf_changed or flows_changed:
                rc = publish(d, D, etf, entry, "T+1 次日复核")
                if rc:
                    return rc
            if hour >= 9 and verified:
                st["morning_verified"] = True
                st["done"] = True
            etf["sz_checked_at"] = now
            save_json(STATE, all_state)
            print(f"[{now}] D={D} | 早间复核完成 verified={int(verified)} "
                  f"etf_changed={int(etf_changed)} flows_changed={int(flows_changed)} "
                  f"stage={'done' if st.get('done') else ('not-verified' if hour >= 9 else 'pending-09')}")
            return 0
        # 晚间没settle（沪市未发布/深市仍在变）→ 落到下面的常规路径继续抓。
        print(f"[{now}] D={D} | 早间复核前置未满足（沪={int(sh_done)} sz_final={int(sz_final_before)}），继续常规抓取")

    # 夜间静默（02-07 点）：晚间已确认双市场后不必再打交易所，等 08/09 点复核。
    if (not is_morning) and 2 <= hour < 8 and sh_done and sz_final_before:
        print(f"[{now}] D={D} 晚间快照已确认（沪 {entry.get('sh_yi')} / 深 {entry.get('sz_yi')}），"
              f"等待 08/09 点早间复核（T+1 可能修订）")
        return 0

    # 1) 沪市：未完成时先探测是否发布；同时存逐只快照供次日复核
    sh_new = None
    codes_sh = etf.get("sh_broad_codes") or []
    if not sh_done and Dprev:
        cur = sse_snapshot(D)
        if not cur:
            print(f"[{now}] D={D} | 沪市: 未发布 | 深市: final={sz_final_before}")
        else:
            prev = sse_snapshot(Dprev)
            if not prev:
                print(f"[{now}] D={D} | 沪市: 当日已发布但缺前一日，无法算差额")
            else:
                if not codes_sh:
                    codes_sh = [c for c, (n, _) in cur.items()
                                if c.startswith("5") and any(k in n for k in C.BROAD_ETF_KEYWORDS)]
                cur_s = {c: v[1] for c, v in cur.items()}
                prev_s = {c: v[1] for c, v in prev.items()}
                prices_sh = tencent_prices(codes_sh, "sh")
                sh_new = delta_from_snap(cur_s, prev_s, codes_sh, prices_sh)
                save_sh_snapshot(D, cur_s)
                if sh_new is not None:
                    save_price_basis(D, sh=prices_sh)

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
                # 价格基准即使「今日没变化」也要落盘：早间复核若发现交易所修订，
                # 必须用晚间同一套价格复算（不能用次日实时价）。
                basis = (load_json(PRICE_BASIS, {}) or {}).get(D) or {}
                prices_sz = basis.get("sz")
                if not prices_sz:
                    prices_sz = tencent_prices(codes_sz, "sz")
                    if prices_sz:
                        save_price_basis(D, sz=prices_sz)
                if sz_diff:
                    new_sz = compute_sz_delta(D, snap, cache, codes_sz, prices=prices_sz)
                    if new_sz is not None:
                        cache[D] = snap
                        save_json(SZ_CACHE, cache)
                        entry["sz_yi"] = new_sz
                        etf["sz_snapshot_at"] = f"{now_dt:%m-%d %H:%M}"
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
        etf["sh_published_at"] = f"{now_dt:%m-%d %H:%M}"

    etf["sz_final"] = bool(st.get("sz_final"))
    etf["sz_checked_at"] = now

    sz_final_now = bool(st.get("sz_final"))
    sh_done = entry.get("sh_yi") is not None
    final = sh_done and sz_final_now
    # 2026-11-26: 这里不再 st["done"]=True —— 晚间「连续不变」实测不是终值，
    # done 只在次日 09 点早间复核后置位（见 morning_recheck / 文件头 Changelog）。
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
