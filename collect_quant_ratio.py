#!/usr/bin/env python3
"""quant-ratio 投资看板数据收集 v5（2026-11-09 模块①下线 + 国家队口径重写）

v5 变更:
1. **下线量化集中度模块** —— 实证：换手率对未来 20 日收益/波动/回撤的相关系数
   为 -0.06~-0.01，对"适合高抛低吸"零预测力。留着会误导决策。
2. **国家队资金改用真代理** —— 旧版把"宽基ETF成交额"当国家队入市；实测同日
   成交 147.7亿 vs 份额净申购 0.42亿，差 350 倍。改为「ETF 份额日变化 × 价格 = 净申购」，
   这才是汇金/证金申购 ETF 的直接体现（沪市覆盖全市场宽基份额 85%）。
3. 成交额降为背景数字（明确标注含二级换手），不再作为信号、不再回算历史序列
   （旧版用"今日成交额 Top40"固定成分回算 21 天，成分随日漂移，跨日不可比）。
4. 信号：ETF 改用净申购分位；主力仍 |z|≥1；北向停止披露不占位。

v5.1 (2026-11-23) 主力资金源修复:
- 东财 `push2his.../fflow/daykline/get` 对本网络稳定空响应（akshare 1.18/1.20 仍只用它），
  2026-11-23 起主力序列整段为空、页面显示「数据缺失」。
- 改为自抓 + 双路径降级 daykline → kline（同 host 同口径，实测 11-19/11-20 数值完全一致），
  Session(trust_env=False) 直连；两路都失败时写 flows.main_flow_error 留痕。
- 采集顺序改为 flow 先、ETF 后：ETF 的 akshare 分页 + 逐页重试最多几十次请求，
  先跑会把东财 IP 打进风控，轮到 flow 时必空。
- 告警: verify_quant 对「序列为空且无 main_flow_error」判 error（静默丢数），有 error 仍只 warn。

v5.2 (2026-11-24) 真根因: IPv6 路径不可用
- 2026-11-24 18:31 日更再次全空，错误与两条路径都一致（RemoteDisconnected）。
  实测: `push2his` 的 AAAA 记录（2409:8c1e:… / 2408:870c:…）TLS 可握手但请求
  必被空响应；`curl -4` 3/3 成功、`curl -6` 0/3。getaddrinfo 把 IPv6 排在前，
  requests/urllib3 默认先走 IPv6 → Python 侧无论 daykline 还是 kline 都必失败；
  昨天的双路径降级因此从未真正生效（成功那次是手工 curl 的 IPv4）。
- 修复: 进程级 `urllib3.util.connection.allowed_gai_family = AF_INET`，
  并取消同路径的第二次重试（该端点对短时多请求敏感）。修复后首次补抓即成功。
- 兜底: `backfill_quant_flow.py` 晚间 21/22/23 点各补一次（已有数据则零成本退出），
  避免单次 18:30 撞上瞬时风控就整天无数据。

数据源（全部 akshare 免费接口）:
- 宽基ETF份额   fund_etf_scale_sse（沪市，按日）
- ETF行情/价格  fund_etf_spot_em
- 主力资金      stock_market_fund_flow
- 北向          stock_hsgt_fund_flow_summary_em（已停止披露）
- 交易日历      tool_trade_date_hist_sina
"""

import sys

sys.path.insert(0, "/Users/jren/hermes-agent/.hermes/bin")  # noqa: F401 (env compat)

import json
import os
import random
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import akshare as ak

# ── 强制 IPv4（2026-11-24 实测）──
# push2his 的 AAAA（2409:8c1e:…/2408:870c:…）TLS 可握手但请求必被空响应；
# getaddrinfo 返回 IPv6 在前 → requests/urllib3 默认先走 IPv6，表现为
# RemoteDisconnected。curl -4 3/3 成功、curl -6 0/3；同 URL 换 IPv4 即恢复。
# 全局 patch 会影响本进程所有 urllib3 请求（新浪/腾讯等 IPv4 也正常）。
try:
    from urllib3.util import connection as _u3conn
    _u3conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:  # noqa: BLE001
    pass

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")
VAULT_DIGEST_DIR = os.path.expanduser("~/Documents/Obsidian Vault/raw/articles/groundapi")

BROAD_ETF_KEYWORDS = ["沪深300", "上证50", "中证500"]
AI_ETF_KEYWORDS = ['人工智能', 'AI', '算力', '大数据', '云计算', '软件', '芯片', '半导体', '通信', '5G']
HIST_TRADING_DAYS = 21
MAX_WORKERS = 6
NEWS_PRIORITY = ['Finance', 'Market', 'Policy', 'AI', 'International', 'Business', 'DeepTech']
FLOW_CACHE = os.path.join(DASHBOARD_PATH, 'cache', 'market_flow.json')

# 东财连接级风控熔断：任一次 RemoteDisconnected 后，本进程内后续东财请求直接跳过降级。
# 背景（2026-11-23）：flow 失败后 ETF 仍逐页重试 ~40 次，把 IP 打进风控，形成「一次失败→
# 风暴→下一轮全失败」的自激循环；同一轮里东财既然已断，再打只会加重封禁。
# 2026-11-25：熔断状态持久化到文件 → 跨进程生效（盘中监控每个整点是独立 cron 进程，
# 原内存版熔断对下一轮完全无效，导致每 30min 反复撞风控）。
_EM_DOWN_UNTIL = [0.0]
_EM_DOWN_FILE = os.path.expanduser("~/.hermes/state/em_fengkong.json")


def _em_mark_down(seconds=180):
    until = time.time() + seconds
    _EM_DOWN_UNTIL[0] = until
    try:
        os.makedirs(os.path.dirname(_EM_DOWN_FILE), exist_ok=True)
        with open(_EM_DOWN_FILE, "w", encoding="utf-8") as fh:
            json.dump({"until": until,
                       "marked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "seconds": seconds}, fh)
    except Exception:  # noqa: BLE001 — 落盘失败不阻断主流程，退回进程内熔断
        pass


def _em_down():
    if time.time() < _EM_DOWN_UNTIL[0]:
        return True
    try:  # 跨进程：其他进程（如盘中监控）可能刚触发熔断
        with open(_EM_DOWN_FILE, encoding="utf-8") as fh:
            until = float(json.load(fh).get("until", 0))
        if time.time() < until:
            _EM_DOWN_UNTIL[0] = until
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _safe_float(x, default=None):
    try:
        v = float(x)
        return v if v == v else default      # NaN → default
    except (TypeError, ValueError):
        return default


def _retry(fn, *args, attempts=3, **kwargs):
    """指数退避重试（0.8s → 1.6s → 3.2s…＋抖动，上限 8s）。

    2026-11-23 事故：东财 push2his 对密集重放直接 RemoteDisconnected；原来的等距短重试
    （0.8s/1.6s）叠加上层多轮重跑 → 3 轮 × 10 次，反而把限流踩满，主力序列全空。
    改成指数退避 + 抖动，并让调用方按数据重要性选 attempts（主力用 2，别再叠风暴）。
    """
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            if i == attempts - 1:
                break
            time.sleep(min(8.0, 0.8 * (2 ** i)) + random.uniform(0, 0.4))
    raise last


def _fetch_etf_spot_resilient(page_retries=10):
    """分页容错的 ETF 实时行情抓取（collect_etf 的降级路径）。

    akshare.fund_etf_spot_em 内部用 fetch_paginated_data 逐页拉取，任一页命中
    东财边缘坏节点 / 触发按 IP 软限流（"Remote end closed connection without
    response"）就会整段 abort。这里改为：
      * 大分页（pz=500，1667 只 ETF ≈ 4 页）——减少往返次数 = 降低命中坏窗口概率；
      * 每页独立重试且退避拉开（指数 + 抖动，单页最坏 ~1min），只重拉失败那页；
      * Session(trust_env=False) 强制直连 —— 避免继承已挂的本地代理 env。
    page_retries 仍失败的页才抛错（如实失败，不静默吞掉，见 R9）。

    返回列：代码/名称/最新价/成交额/最新份额 —— collect_etf 仅用这 5 列。
    """
    import math
    import random

    import pandas as pd
    import requests

    url = "https://88.push2.eastmoney.com/api/qt/clist/get"
    # 多个边缘子域轮流用——不同 subdomain 常对应独立边缘池/限流计数，
    # 单页重试时轮换 host 提高命中健康节点的概率。
    urls = [
        "https://88.push2.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://90.push2.eastmoney.com/api/qt/clist/get",
    ]
    base_params = {
        "pn": 1, "pz": 500, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "wbp2u": "|0|0|0|web",
        "fid": "f3",
        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
        "fields": "f2,f6,f12,f14,f38",
    }
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    sess = requests.Session()
    sess.trust_env = False          # 强制直连，不读 HTTP(S)_PROXY env（本地代理可能已挂）

    def get_page(pn):
        params = dict(base_params, pn=pn)
        last = None
        for attempt in range(page_retries):
            try:
                r = sess.get(urls[attempt % len(urls)], params=params, timeout=20, headers=headers)
                r.raise_for_status()
                data = (r.json().get("data") or {})
                diff = data.get("diff")
                if not isinstance(diff, list):
                    raise ValueError(f"page {pn}: 响应缺 data.diff")
                return diff, data.get("total")
            except Exception as e:  # noqa: BLE001
                last = e
                backoff = min(2 ** attempt * 1.0, 15.0) + random.uniform(0, 1.5)
                time.sleep(backoff)
        raise RuntimeError(f"page {pn} 重试 {page_retries} 次仍失败: {last}")

    diff1, total = get_page(1)
    rows = list(diff1)
    per_page = len(diff1) or base_params["pz"]
    total_pages = math.ceil(total / per_page) if (total and isinstance(total, int)) else 1
    for pn in range(2, total_pages + 1):
        time.sleep(random.uniform(2.0, 3.5))
        diff, _ = get_page(pn)
        rows.extend(diff)

    df = pd.DataFrame(rows).rename(columns={
        "f12": "代码", "f14": "名称", "f2": "最新价",
        "f6": "成交额", "f38": "最新份额"})
    return df


# ---------------- 交易日历 ----------------
def get_trading_dates(today_iso):
    """返回 (<=今日的最近21个交易日, 今日是否交易日)。非交易日不得采集。"""
    try:
        df = _retry(ak.tool_trade_date_hist_sina)
        cal = sorted({str(x) for x in df['trade_date']})
        dates = [d for d in cal if d <= today_iso][-HIST_TRADING_DAYS:]
        if dates:
            return dates, (today_iso in cal)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 交易日历获取失败: {e}")
    return [], False


# ---------------- 国家队资金：ETF 份额净申购 ----------------
SZ_CACHE = os.path.join(DASHBOARD_PATH, "cache", "etf_shares_szse.json")


def _load_sz_cache():
    try:
        with open(SZ_CACHE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_sz_cache(cache):
    os.makedirs(os.path.dirname(SZ_CACHE), exist_ok=True)
    for d in sorted(cache)[:-60]:        # 只留最近 60 个快照日
        cache.pop(d, None)
    with open(SZ_CACHE, 'w') as f:
        json.dump(cache, f, ensure_ascii=False)


def collect_etf(trading_dates, today_iso):
    out = {'broad_count': None, 'broad_today_yi': None,
           'netbuy_series': [], 'netbuy_today_yi': None,
           'netbuy_coverage_pct': None, 'netbuy_basis': '',
           'sz_tracked_days': 0, 'sz_tracked_since': None,
           'ai_union_count': None, 'ai_union_amt_yi': None}

    # ETF 实时行情：优先走 akshare（正常日快路径）；东财边缘节点偶发断连导致
    # 其内部整段分页 abort 时，降级到「每页独立重试」的容错抓取。
    spot = None
    used_sina = False
    if _em_down():
        # 主力资金已判定东财熔断 → 不再逐页重试（~40 次请求会让 IP 封得更久），直接降级新浪
        print("   ⚠️ 东财本轮已熔断，ETF 行情直接降级新浪源")
    else:
        try:
            spot = _retry(ak.fund_etf_spot_em, attempts=2)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ fund_etf_spot_em 分页失败，启用逐页容错重试: {e}")
            try:
                spot = _fetch_etf_spot_resilient()
            except Exception as e2:  # noqa: BLE001
                _em_mark_down()
                print(f"   ⚠️ 东财两条路径均失败({type(e2).__name__})，降级新浪源(无份额列): {e2}")
    if spot is None:
        import pandas as _pd
        s = _retry(ak.fund_etf_category_sina, symbol="ETF基金", attempts=3)
        # 新浪代码带 sh/sz 前缀 → 去掉，与 SSE/SZSE 的 6 位裸码对齐
        code = s['代码'].astype(str).str.replace(r'^(sh|sz)', '', regex=True)
        spot = _pd.DataFrame({
            '代码': code, '名称': s['名称'], '最新价': s['最新价'],
            '成交额': s['成交额'], '最新份额': float('nan'),
        })
        used_sina = True
    spot['代码'] = spot['代码'].astype(str)
    names = dict(zip(spot['代码'], spot['名称'].astype(str)))
    price = dict(zip(spot['代码'], spot['最新价'].apply(_safe_float)))
    shares_today = dict(zip(spot['代码'], spot['最新份额'].apply(_safe_float)))
    amt_today = dict(zip(spot['代码'], spot['成交额'].apply(_safe_float)))

    broad = [c for c, n in names.items() if any(k in n for k in BROAD_ETF_KEYWORDS)]
    sh_broad = [c for c in broad if c.startswith('5')]
    sz_broad = [c for c in broad if not c.startswith('5')]
    # 落盘成分清单：次日「沪市补抓」按同一批代码复算，避免两套口径
    out['sh_broad_codes'] = sorted(sh_broad)
    out['sz_broad_codes'] = sorted(sz_broad)
    out['broad_count'] = len(broad)
    out['broad_today_yi'] = round(sum(amt_today.get(c) or 0 for c in broad) / 1e8, 2)

    # 沪市：交易所按日份额（可回溯）
    def fetch_sh(d):
        try:
            df = _retry(ak.fund_etf_scale_sse, date=d.replace('-', ''))
            df.columns = ['序号', '代码', '简称', '类型', '统计日', '份额']
            return d, {str(c): _safe_float(v) for c, v in zip(df['代码'], df['份额'])}
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 沪市份额 {d} 获取失败: {e}")
            return d, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        shares_by_date = {d: s for d, s in ex.map(fetch_sh, trading_dates) if s}

    # 深市：交易所快照 + 本地逐日缓存（从首次运行那天起累积，无法回溯）
    sz_cache = _load_sz_cache()
    try:
        szdf = _retry(ak.fund_etf_scale_szse)
        szdf['基金代码'] = szdf['基金代码'].astype(str)
        szset = set(sz_broad)
        snap = {c: _safe_float(v) for c, v in zip(szdf['基金代码'], szdf['基金份额'])
                if c in szset and _safe_float(v) is not None}
        if snap:
            sz_cache[today_iso] = snap
            _save_sz_cache(sz_cache)
        sh_snap = shares_by_date.get(today_iso) or {}
        out['sz_tracked_days'] = len(sz_cache)
        out['sz_tracked_since'] = min(sz_cache) if sz_cache else None
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ 深市份额快照失败: {e}")
        sh_snap = shares_by_date.get(today_iso) or {}

    sz_dates = sorted(sz_cache)

    def sz_delta(d):
        """深市当日净申购（亿元）—— 需要缓存里有 d 与之前最近的快照"""
        if d not in sz_cache:
            return None
        prevs = [x for x in sz_dates if x < d]
        if not prevs:
            return None
        cur, prev = sz_cache[d], sz_cache[prevs[-1]]
        amt = 0.0
        for c, s1 in cur.items():
            s0, px = prev.get(c), price.get(c)
            if s0 is None or not px:
                continue
            amt += (s1 - s0) * px
        return round(amt / 1e8, 2)

    netbuy = []
    for i, d in enumerate(trading_dates):
        cur = shares_by_date.get(d)
        prev = shares_by_date.get(trading_dates[i - 1]) if i > 0 else None
        sh_amt = None
        if cur and prev:
            acc = 0.0
            for c in sh_broad:
                s1, s0, px = cur.get(c), prev.get(c), price.get(c)
                if s1 is None or s0 is None or not px:
                    continue
                acc += (s1 - s0) * px
            sh_amt = round(acc / 1e8, 2)
        sz_amt = sz_delta(d)
        if sh_amt is None and sz_amt is None:
            netbuy.append({'date': d, 'net_yi': None, 'sh_yi': None, 'sz_yi': None,
                           'complete': False})
        else:
            # complete=False = 只有单侧市场（另一侧未发布/未累积）→ 该日不进分位基线、
            # 也不允许触发信号。2026-11-23 事故：沪市当日未发布，P84 用「深市单口径」
            # 去比「沪深合计」历史，得到一个看似触发实则混口径的信号。
            complete = sh_amt is not None and sz_amt is not None
            total = round((sh_amt or 0) + (sz_amt or 0), 2)
            netbuy.append({'date': d, 'net_yi': total, 'sh_yi': sh_amt, 'sz_yi': sz_amt,
                           'complete': complete})
    out['netbuy_series'] = netbuy
    out['netbuy_today_yi'] = next((p['net_yi'] for p in netbuy if p['date'] == today_iso), None)
    today_entry = next((p for p in netbuy if p['date'] == today_iso), None)
    out['netbuy_today_complete'] = bool(today_entry and today_entry.get('complete'))
    out['netbuy_today_missing'] = [] if out['netbuy_today_complete'] else [
        m for m, v in (('沪市', (today_entry or {}).get('sh_yi')),
                       ('深市', (today_entry or {}).get('sz_yi'))) if v is None]

    # 覆盖率 = 今日能算出「变化」的宽基份额 / 全市场宽基份额
    # 深市要等到有前一日快照才能算变化，所以首日覆盖率=沪市占比（~85%），次日起 ~100%
    tot = sum(shares_today.get(c) or 0 for c in broad)
    cov_sh = sum(sh_snap.get(c) or 0 for c in sh_broad)
    sz_countable = today_entry is not None and today_entry.get('sz_yi') is not None
    cov_sz = sum((sz_cache.get(today_iso) or {}).get(c) or 0 for c in sz_broad) if sz_countable else 0
    # 降级源（新浪）无份额列 → tot=0；此时分母改用「交易所已追踪全集」，口径自洽
    if not tot and (cov_sh or cov_sz):
        tot = cov_sh + cov_sz
    out['netbuy_coverage_pct'] = round((cov_sh + cov_sz) / tot * 100, 1) if tot else None
    out['sz_delta_available'] = sz_countable
    out['netbuy_basis'] = (
        f"宽基ETF份额净申购 = 沪市（上交所按日，{len(sh_broad)}只，T+1 发布：官方口径约次日 23:00 更新，"
        f"当日 18:30 日更通常没有）+ 深市（深交所当日快照，{len(sz_broad)}只，"
        f"自 {out['sz_tracked_since'] or today_iso} 起累积，已 {out['sz_tracked_days']} 个快照日）× 最新价；"
        f"当前覆盖 {out['netbuy_coverage_pct']}% 宽基份额"
        + ('' if sz_countable else '（深市自次日起可算变化）')
        + ('；⚠️ 行情源降级为新浪(东财限流)，覆盖率分母=交易所已追踪全集' if used_sina else ''))

    ai = [c for c, n in names.items() if any(k in n for k in AI_ETF_KEYWORDS)]
    out['ai_union_count'] = len(ai)
    out['ai_union_amt_yi'] = round(sum(amt_today.get(c) or 0 for c in ai) / 1e8, 2)

    sz_today = next((p['sz_yi'] for p in netbuy if p['date'] == today_iso), None)
    print(f"✅ ETF: 宽基{len(broad)}只 今日成交{out['broad_today_yi']}亿 | "
          f"净申购 沪{next((p['sh_yi'] for p in netbuy if p['date'] == today_iso), None)}亿 + "
          f"深{sz_today if sz_today is not None else '未积累'}亿 = {out['netbuy_today_yi']}亿 "
          f"（覆盖{out['netbuy_coverage_pct']}%份额）")
    return out


# ---------------- 主力资金 + 北向 ----------------
def _fetch_market_fund_flow():
    """东财全市场主力资金日频序列（直连，不读代理 env）。

    2026-11-23 起 push2his 的 `/fflow/daykline/get` 对本网络**稳定空响应**
    （同 host 的 `/fflow/kline/get` 正常；akshare 1.18/1.20 仍只用 daykline），
    表现为 RemoteDisconnected / Empty reply → stock_market_fund_flow 整段失败。
    这里按路径降级：daykline（15 列，含主力净占比）→ kline（6 列，仅净额），
    字段按下标解析，返回 DataFrame[日期, 主力净流入-净额, 主力净流入-净占比]。
    """
    import pandas as pd
    import requests

    params = {
        "lmt": "0", "klt": "101",
        "secid": "1.000001", "secid2": "0.399001",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": int(time.time() * 1000),
    }
    sess = requests.Session()
    sess.trust_env = False          # 直连：本地代理常挂，国内接口无需经代理

    if _em_down():
        raise RuntimeError("东财本轮已熔断（前序请求触发连接级风控），跳过以免加重封禁")

    last = None
    for path in ("daykline", "kline"):
        for attempt in range(1):   # 每路径只打 1 次：该端点对短时多请求很敏感
            try:
                r = sess.get(
                    f"https://push2his.eastmoney.com/api/qt/stock/fflow/{path}/get",
                    params=params, timeout=20,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
                r.raise_for_status()
                body = r.json()
                klines = (body.get("data") or {}).get("klines") or []
                if not klines:
                    # rc!=0（如 102）= 东财限流/风控的空响应，不是网络断连
                    raise ValueError(f"{path}: 响应无 klines（rc={body.get('rc')}"
                                     + ("，疑似限流/风控" if body.get("rc") not in (0, None) else "") + "）")
                rows = []
                for line in klines:
                    f = line.split(",")
                    if len(f) < 2:
                        continue
                    rows.append({
                        "日期": f[0],
                        "主力净流入-净额": _safe_float(f[1]),
                        # f57=主力净占比；kline 路径只回 6 列，无占比
                        "主力净流入-净占比": _safe_float(f[6]) if len(f) > 6 else None,
                    })
                df = pd.DataFrame(rows)
                df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.date
                df = df.dropna(subset=["主力净流入-净额"])
                if df.empty:
                    raise ValueError(f"{path}: klines 解析后为空")
                if path != "daykline":
                    print("⚠️ 主力资金 daykline 路径失效，已降级 kline 路径（同源同口径）")
                return df
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt == 0:
                    time.sleep(0.8 + random.uniform(0, 0.4))
    # 两条路径都断 = 不是路径差异而是 IP/主机级风控 → 熔断，阻止后续 ETF 再打东财
    _em_mark_down()
    raise RuntimeError(f"东财主力资金两条路径均失败: {last}")


def _load_flow_cache():
    try:
        with open(FLOW_CACHE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _save_flow_cache(series):
    os.makedirs(os.path.dirname(FLOW_CACHE), exist_ok=True)
    with open(FLOW_CACHE, 'w', encoding='utf-8') as fh:
        json.dump({'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
                   'series': series}, fh, ensure_ascii=False)


def collect_flows(include_northbound=True, save_cache=True):
    """save_cache=False：早间复核只用于比对/回填，不写「当日已抓」缓存 ——
    否则 18:30 抓取失败时会拿早间的盘中半成品当收盘值兜底。"""
    out = {'main_flow_series': [], 'northbound': None}
    try:
        # 每路径只打 1 次 → 不再叠 _retry，避免限流风暴
        df = _fetch_market_fund_flow()
        for _, r in df.tail(HIST_TRADING_DAYS).iterrows():
            out['main_flow_series'].append({
                'date': str(r['日期'])[:10],
                'net_yi': round(_safe_float(r.get('主力净流入-净额'), 0) / 1e8, 2),
                'pct': _safe_float(r.get('主力净流入-净占比')),
            })
        if out['main_flow_series'] and save_cache:
            _save_flow_cache(out['main_flow_series'])
    except Exception as e:  # noqa: BLE001
        # 留痕：verify_quant 用它在「序列为空」时区分外部断连 vs 静默丢数
        out['main_flow_error'] = f"{type(e).__name__}: {str(e)[:160]}"
        # 同日重跑兜底：本次失败但当天早些时候已成功抓过 → 复用当日缓存，
        # 避免后一轮把前一轮的好数据覆盖成空（2026-11-23 页面被清空的直接机制）。
        cache = _load_flow_cache() or {}
        series = cache.get('series') or []
        if series and series[-1].get('date') == date.today().isoformat():
            out['main_flow_series'] = series
            out['main_flow_cached_at'] = cache.get('fetched_at')
            out.pop('main_flow_error', None)
            print(f"✅ 主力资金: 本次抓取失败，复用当日已抓缓存"
                  f"（{cache.get('fetched_at')}，{len(series)}日）")
        else:
            print(f"⚠️ 主力资金流获取失败: {e}")
    if include_northbound:      # 补抓任务只需主力资金，少打一次东财
        try:
            df_nb = _retry(ak.stock_hsgt_fund_flow_summary_em)
            nb = df_nb[df_nb['资金方向'] == '北向'] if len(df_nb) else None
            if nb is not None and len(nb):
                out['northbound'] = {'date': str(nb.iloc[0]['交易日'])[:10],
                                     'netbuy_yi': round(sum(_safe_float(x, 0) for x in nb['成交净买额']) / 1e8, 2),
                                     'note': '2024-08 起不再披露实时净买额'}
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 北向资金获取失败: {e}")
    if out['main_flow_series']:
        print(f"✅ 主力资金: 今日{out['main_flow_series'][-1]['net_yi']}亿")
    return out


# ---------------- 财经要闻 ----------------
def collect_news_digest():
    """按主题优先级合并当日 digest（旧版按字典序取最后一个 = Unclassified）"""
    result = {'date': None, 'themes_points': [], 'items': [], 'source_files': []}
    if not os.path.isdir(VAULT_DIGEST_DIR):
        print("⚠️ 未找到 digest 目录")
        return result
    today_iso = date.today().strftime('%Y%m%d')
    all_files = [f for f in os.listdir(VAULT_DIGEST_DIR) if re.match(r'^\d{8}_.+\.md$', f)]
    todays = {f[9:].rsplit('.md', 1)[0]: f for f in all_files if f.startswith(today_iso)}
    if not todays:
        latest = sorted(all_files, reverse=True)[:1]
        if not latest:
            return result
        todays = {latest[0][9:].rsplit('.md', 1)[0]: latest[0]}
    picked = [todays[t] for t in NEWS_PRIORITY if t in todays] or list(todays.values())
    result['source_files'] = picked
    result['date'] = picked[0][:8]
    # 要闻是早间管道产出的，collect 往往在傍晚/夜间 → 记录源文件最后写入时刻，
    # 看板标题行据此显示真实「刷新时间」，而不是本次 collect 时间。
    try:
        mtime = max(os.path.getmtime(os.path.join(VAULT_DIGEST_DIR, f)) for f in picked)
        result['generated_at'] = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
    except Exception:  # noqa: BLE001
        pass

    seen_urls, seen_points = set(), set()
    for fname in picked:
        try:
            text = open(os.path.join(VAULT_DIGEST_DIR, fname)).read()
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ digest读取失败({fname}: {e})")
            continue
        cur_theme = fname[9:].rsplit('.md', 1)[0]
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            hm = re.match(r'^#{2,4}\s+(.+?)\s*(?:\(\d+\s*条\))?\s*$', lines[i])
            if hm:
                cur_theme = hm.group(1).strip()
                i += 1
                continue
            if '🧠 AI 归纳要点' in lines[i]:
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                while j < len(lines):
                    lm = re.match(r'^\s*[-•]\s+(.+?)\s*$', lines[j])
                    if not lm:
                        break
                    pt = lm.group(1)[:200]
                    if pt not in seen_points and len(result['themes_points']) < 12:
                        seen_points.add(pt)
                        result['themes_points'].append({'theme': cur_theme, 'point': pt})
                    j += 1
                i = j
                continue
            im = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', lines[i])
            if im and len(result['items']) < 12 and im.group(2) not in seen_urls:
                seen_urls.add(im.group(2))
                result['items'].append({'title': im.group(1).strip()[:80], 'url': im.group(2)})
            i += 1
    print(f"✅ 财经要闻: {len(picked)}个主题文件 | AI要点{len(result['themes_points'])}条, 条目{len(result['items'])}条")
    return result


# ---------------- 信号 ----------------
def _pct_rank(hist, value):
    h = [x for x in hist if x is not None]
    if not h or value is None:
        return None
    return round(sum(1 for x in h if x < value) / len(h) * 100)


def _quantile(hist, q):
    h = sorted(x for x in hist if x is not None)
    if len(h) < 5:
        return None
    return round(h[min(len(h) - 1, max(0, int(round(q * (len(h) - 1)))))], 1)


def evaluate_signals(etf, flows):
    sigs = []

    def add(name, current, unit, triggered, *, percentile=None, zscore=None,
            baseline='', direction='', note='', data_incomplete=False):
        sigs.append({'name': name, 'current': current, 'unit': unit, 'triggered': bool(triggered),
                     'percentile': percentile, 'zscore': zscore, 'baseline': baseline,
                     'threshold': baseline, 'direction': direction if triggered else '', 'note': note,
                     'data_incomplete': bool(data_incomplete)})

    series = etf.get('netbuy_series') or []
    # 分位基线只取「沪深两侧齐全」的历史日：单口径(仅深/仅沪)的点与合计口径不可比
    hist = [p['net_yi'] for p in series[:-1] if p.get('complete', True)]
    today = etf.get('netbuy_today_yi')
    complete_today = etf.get('netbuy_today_complete', True)
    missing = etf.get('netbuy_today_missing') or []
    p = _pct_rank(hist, today) if complete_today else None
    p80 = _quantile(hist, 0.8) if complete_today else None
    add('国家队ETF净申购（沪深合计）', today, '亿', (p is not None and p >= 80),
        percentile=p,
        baseline=(f'近{len([h for h in hist if h is not None])}日80分位 {p80}亿' if p80 is not None
                  else ('今日数据不完整' if not complete_today else '历史不足')),
        direction=f'国家队大额申购（P{p}，{today}亿）' if (p is not None and p >= 80) else '',
        note='份额日变化×价格；沪市 T+1 发布、深市当日；ETF成交额含二级换手，不是净申购'
             + (f'；⚠️ 今日缺 {"、".join(missing) or "单侧市场"} → 标记 incomplete，不计分位、不触发'
                if not complete_today else ''),
        data_incomplete=not complete_today)

    mf = [x.get('net_yi') for x in (flows.get('main_flow_series') or [])]
    if mf:
        today_mf, hist_mf = mf[-1], mf[:-1]
        clean = [h for h in hist_mf if h is not None]
        mu = sum(clean) / max(1, len(clean))
        sd = (sum((x - mu) ** 2 for x in clean) / max(1, len(clean))) ** 0.5
        z = round((today_mf - mu) / sd, 2) if sd else None
        add('主力净流入', today_mf, '亿', (z is not None and abs(z) >= 1.0),
            percentile=_pct_rank(hist_mf, today_mf), zscore=z,
            baseline=f'近{len(clean)}日均值 {round(mu, 1)}亿',
            direction=('主力资金异动加仓' if (today_mf or 0) > 0 else '主力资金异动撤离')
                      if (z is not None and abs(z) >= 1.0) else '',
            note='东财「超大单+大单」净额，按单笔金额分类，非机构身份')
    else:
        add('主力净流入', None, '亿', False, note='数据缺失（东财源断连，已指数退避重试）',
            data_incomplete=True)

    sigs.append({'name': '北向单日净买额', 'current': None, 'unit': '亿', 'triggered': False,
                 'percentile': None, 'zscore': None, 'baseline': '停止披露',
                 'threshold': '停止披露', 'direction': '',
                 'note': '2024-08 起不再披露实时净买额，停用'})

    triggered = [s for s in sigs if s['triggered']]
    print(f"✅ 投资信号: {len(triggered)}/{len(sigs)} 触发 → " + "; ".join(s['name'] for s in triggered))
    return {'rules': {'etf_netbuy': '净申购 ≥ 近20日80分位', 'main_flow': '|z|≥1'},
            'signals': sigs, 'triggered_count': len(triggered),
            'push_line': ' | '.join(f"📢 {s['direction'] or s['name']} (当前{s['current']}{s['unit']})"
                                    for s in triggered)}


# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    print("=== quant-ratio v5: 国家队资金 + 要闻 + 信号 ===\n")
    today_iso = os.environ.get('QUANT_FORCE_DATE') or date.today().isoformat()

    trading_dates, is_trading = get_trading_dates(today_iso)
    if not trading_dates:
        print("❌ 无法获取交易日历，终止")
        sys.exit(1)
    if not is_trading:
        print(f"⏸ {today_iso} 非交易日（最近 {trading_dates[-1]}），跳过")
        return
    print(f"→ 交易日窗口: {trading_dates[0]} → {trading_dates[-1]} ({len(trading_dates)}日)")

    # 先取主力资金（1 次请求），再取 ETF（akshare 分页 + 逐页重试，最多几十次请求）。
    # 反了会先触发东财 IP 级风控，等轮到 flow 时已被封 → 主力序列全空（2026-11-23）。
    flows = collect_flows()
    etf = collect_etf(trading_dates, today_iso)
    news = collect_news_digest()
    signals = evaluate_signals(etf, flows)

    data = {
        'version': 5,
        'date': today_iso,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'trading_dates_window': [trading_dates[0], trading_dates[-1]],
        'modules': ['国家队资金动向', '今日财经要闻', '投资信号'],
        'offline_modules': {'量化集中度': '2026-11-09 下线：换手率对未来20日收益/波动/回撤相关系数 -0.01~-0.06，无预测力'},
        'etf': etf,
        'flows': flows,
        'news_digest': news,
        'signals': signals,
    }

    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"✅ 数据已保存 {DATA_FILE} ({os.path.getsize(DATA_FILE) / 1024:.0f} KB)")

    os.makedirs(HISTORY_DIR, exist_ok=True)
    theme = sys.argv[1] if len(sys.argv) > 1 else "quant"
    with open(os.path.join(HISTORY_DIR, f"{theme}_{today_iso}.json"), 'w') as f:
        json.dump(data, f, ensure_ascii=False)

    with open(os.path.join(DASHBOARD_PATH, "quant_push_signal.txt"), 'w') as f:
        f.write(signals['push_line'] if signals['triggered_count'] else "(无触发)")
    print("SIGNAL_PUSH: " + (signals['push_line'] if signals['triggered_count'] else "(无触发)"))
    print(f"\n=== v5 完成, 耗时 {int(time.time() - t0)}s ===")


if __name__ == "__main__":
    main()
