#!/usr/bin/env python3
"""quant-ratio 投资看板数据收集 v3 — 四模块真实数据源（2026-11-09 按用户原始需求重写）

用户需求（会话确认版）:
① 量化集中度: AI板块 + 全市场Top/Bottom10板块, 历史趋势=量化集中度指标本身
   (对比上一交易日 / 近5/10/20个交易日), 每板块 top5/bottom5 个股及趋势
② 国家队资金动向: 宽基ETF成交额(国家队代理) + 主力净流入 + 北向, 阈值触发推送
③ 今日财经要闻: 复用 Sina News digest (vault raw/articles/groundapi/)
④ 投资信号: 阈值触发的买入/关注信号列表

数据源（全部 akshare 免费接口, 真实值）:
- 板块成分股+换手率   stock_board_industry_cons_em / name_em
- 个股历史换手率      stock_zh_a_hist (带缓存 cache/stock_turnover.json, 增量更新)
- ETF行情/历史        fund_etf_spot_em / fund_etf_hist_em
- 主力资金            stock_market_fund_flow
- 北向资金            stock_hsgt_fund_flow_summary_em
- 财经要闻            vault digest 文件

指标口径:
- 板块量化集中度(代理) = 成分股中当日换手率>3%的个股占比 % (skill标准, 阈值透明可调)
- 个股量化活跃度(代理) = 该股当日换手率 %
- 历史趋势 = 上述指标在历史交易日的逐日值 + 近5/10/20日均值对比
"""

import sys
sys.path.insert(0, "/Users/jren/hermes-agent/.hermes/bin")  # noqa: F401 (env compat)

import akshare as ak
import pandas as pd
from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import time
import threading

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")
CACHE_DIR = os.path.join(DASHBOARD_PATH, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "stock_turnover.json")

# ---------------- 配置（阈值透明, 页面展示） ----------------
ACTIVE_THRESHOLD_PCT = 3.0        # 换手率>此值 → 量化活跃 (skill标准)
MIN_SECTOR_STOCKS = 20            # 全市场排名门槛: 成分股≥20只 (避免微型板块退化值)
HIST_TRADING_DAYS = 21            # 历史窗口(交易日, 含今日): 支持 1/5/10/20 对比
CACHE_KEEP_CALENDAR_DAYS = 60     # 缓存保留日历天数
MAX_WORKERS = 6                   # akshare 并发 (东财限流保护)

# v4: 只保留可交易标的作分母 —— B股/停牌天然低换手，混入会系统性稀释集中度
B_SHARE_PREFIX = ('900', '200')   # 沪B / 深B
MIN_SERIES_COVERAGE = 0.80        # 单日有效成分股覆盖率下限，低于此该日置 None
MIN_AVG_COVERAGE = 0.80           # 均线样本覆盖率下限，不足则不给均值（宁缺勿假）

# v4: 新闻 digest 主题优先级（此前按字典序取到最后一个是 Unclassified，等于没读）
NEWS_PRIORITY = ['Finance', 'Market', 'Policy', 'AI', 'International', 'Business', 'DeepTech']

AI_SECTORS = ["计算机", "电子", "通信设备", "半导体"]
SECTOR_ETF_KEYWORDS = {           # 板块 → ETF名称关键词 (代码去重)
    '计算机': ['人工智能', 'AI', '算力', '大数据', '云计算', '软件'],
    '电子':   ['芯片'],
    '通信设备': ['通信', '5G'],
    '半导体':  ['半导体'],
}
BROAD_ETF_KEYWORDS = ["沪深300", "上证50", "中证500"]  # 国家队宽基代理

THRESHOLDS = {                    # 投资信号阈值 (亿 / %)
    "broad_etf_daily_yi": 30,     # 宽基ETF单日成交额 ≥30亿 → 资金大幅流入(国家队)
    "main_flow_net_yi": 30,       # 主力净流入 ≥+30亿 → 主力加仓; ≤-30亿 → 主力撤离
    "northbound_netbuy_yi": 5,    # 北向单日净买额 |≥5亿| → 外资动向信号
    "ai_active_ratio_pct": 35,    # AI板块量化活跃占比 ≥35% → 量化集中度高 (skill标准)
}

VAULT_DIGEST_DIR = os.path.expanduser("~/Documents/Obsidian Vault/raw/articles/groundapi")


def _safe_float(x, default=0.0):
    try:
        v = float(x)
        return v if pd.notna(v) else default
    except (TypeError, ValueError):
        return default


def _retry(fn, *args, attempts=3, **kwargs):
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


# ---------------- 个股换手率缓存（增量） ----------------
_cache_lock = threading.Lock()
_stock_cache = {}   # code -> {date_str: turnover_pct}

def load_cache():
    global _stock_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                raw = json.load(f)
            cutoff = (date.today() - timedelta(days=CACHE_KEEP_CALENDAR_DAYS)).isoformat()
            for code, series in raw.items():
                _stock_cache[code] = {d: v for d, v in series.items() if d >= cutoff}
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 缓存加载失败({e}), 冷启动")

def save_cache():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump(_stock_cache, f, ensure_ascii=False)
    print(f"✅ 换手率缓存已保存 ({len(_stock_cache)}只个股)")

def backfill_stock(code, start_iso):
    """拉取单只个股历史换手率并合并进缓存。返回该股的日期→换手 dict"""
    with _cache_lock:
        existing = _stock_cache.get(code)
        if existing and len([d for d in existing if d >= start_iso]) >= HIST_TRADING_DAYS - 1 \
           and date.today().isoformat() in existing:
            return existing
    try:
        df = _retry(ak.stock_zh_a_hist, symbol=code, period="daily",
                    start_date=start_iso.replace('-', ''), end_date=date.today().strftime('%Y%m%d'))
    except Exception:  # noqa: BLE001
        return {}
    series = {}
    if df is not None and len(df) > 0 and '换手率' in df.columns:
        for _, r in df.iterrows():
            d = str(r['日期'])[:10]
            t = _safe_float(r.get('换手率'), default=None)
            if t is not None:
                series[d] = round(t, 3)
    with _cache_lock:
        merged = dict(_stock_cache.get(code, {}))
        merged.update(series)
        _stock_cache[code] = merged
    return series


# ---------------- Phase A: 全市场板块扫描（今日快照） ----------------
def scan_all_sectors(today_iso):
    df_b = _retry(ak.stock_board_industry_name_em)
    sector_names = [str(x) for x in df_b['板块名称'].tolist()]
    print(f"→ 全市场行业板块: {len(sector_names)}个, 开始扫描成分股...")

    results = {}
    done = 0
    lock = threading.Lock()

    def work(name):
        try:
            df_cons = _retry(ak.stock_board_industry_cons_em, symbol=name)
        except Exception:  # noqa: BLE001
            return name, None
        if df_cons is None or len(df_cons) == 0:
            return name, None
        to = pd.to_numeric(df_cons.get('换手率'), errors='coerce')
        amt = pd.to_numeric(df_cons.get('成交额'), errors='coerce')
        raw_codes = [str(c) for c in df_cons['代码'].tolist()]
        raw_names = [str(x) for x in df_cons['名称'].tolist()]
        n_total = len(raw_codes)

        # v4: 分母只算可交易标的（剔 B股 + 当日无成交的停牌股）
        idx, b_codes, s_codes = [], [], []
        for i, code in enumerate(raw_codes):
            if code.startswith(B_SHARE_PREFIX):
                b_codes.append(code)
                continue
            if not pd.notna(to.iloc[i]) or not pd.notna(amt.iloc[i]) or float(amt.iloc[i]) <= 0:
                s_codes.append(code)
                continue
            idx.append(i)

        if not idx:
            return name, None
        codes = [raw_codes[i] for i in idx]
        n_tradable = len(codes)
        active = int(sum(1 for i in idx if float(to.iloc[i]) > ACTIVE_THRESHOLD_PCT))

        def _row(i):
            return {'代码': raw_codes[i], '名称': raw_names[i],
                    '换手率%': round(float(to.iloc[i]), 2),
                    '成交额亿': round(float(amt.iloc[i]) / 1e8, 2)}

        ranked = sorted(idx, key=lambda i: float(to.iloc[i]), reverse=True)
        top5 = [_row(i) for i in ranked[:5]]
        bottom5 = [_row(i) for i in reversed(ranked[-5:])]
        pairs = [(raw_codes[i], round(float(to.iloc[i]), 3)) for i in idx]
        return name, {
            'codes': codes,                 # 可交易成分股（历史序列/排名用）
            'n_stocks': n_total,            # 原成分股数（页面展示）
            'n_tradable': n_tradable,       # v4 分母
            'active_count': active,
            'active_ratio_pct': round(active / n_tradable * 100, 1),
            'excluded_b': len(b_codes),
            'excluded_susp': len(s_codes),
            'excluded_b_codes': b_codes,
            'excluded_susp_codes': s_codes,
            'top5_today': top5,
            'bottom5_today': bottom5,
            '_today_pairs': pairs,
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(work, nm): nm for nm in sector_names}
        for fut in as_completed(futs):
            name, res = fut.result()
            if res is not None:
                results[name] = res
                with lock:
                    done += 1
                    if done % 80 == 0:
                        print(f"   ... {done}/{len(sector_names)}")

    # 今日值合并进缓存（主线程, 无竞争）
    for name, res in results.items():
        for code, tv in res.pop('_today_pairs', []):
            with _cache_lock:
                m = dict(_stock_cache.get(code, {}))
                m[today_iso] = tv
                _stock_cache[code] = m
    print(f"✅ 全市场扫描完成: {len(results)}/{len(sector_names)}个板块有成分股数据")
    return results


# ---------------- Phase B/C: 历史趋势（量化集中度指标本身） ----------------
def get_trading_dates(today_iso):
    """真实交易日历。返回 (<=今日的最近21个交易日, 今日是否交易日)

    v4: 不再无条件 append 今日 —— 周末/节假日 append 会把上一交易日的数据盖上今天的日期，
        写进缓存后污染 21 日序列与所有均线（环比恒 0、均值被重复日稀释）。
    """
    try:
        df = _retry(ak.tool_trade_date_hist_sina)
        cal = sorted({str(x) for x in df['trade_date']})
        dates = [d for d in cal if d <= today_iso][-HIST_TRADING_DAYS:]
        if dates:
            return dates, (today_iso in cal)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 交易日历获取失败({e}), 回退主力资金日期轴")
    try:
        df = _retry(ak.stock_market_fund_flow)
        dates = sorted({str(d)[:10] for d in df['日期'].tolist()})[-HIST_TRADING_DAYS:]
        return dates, (today_iso in dates)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 交易日轴回退也失败({e})")
    return [], False


def sector_active_series(codes, trading_dates):
    """逐日计算板块量化集中度(活跃占比%) — 分母=当日有数据的可交易成分股

    v4: 单日覆盖率 < MIN_SERIES_COVERAGE 时该点置 None —— 宁缺勿假，
        避免「只有 3 只股票有数据」也算出一个百分比。
    """
    cache = dict(_stock_cache)          # 浅拷贝快照，避免逐股加锁
    need = max(5, int(len(codes) * MIN_SERIES_COVERAGE))
    series = []
    for d in trading_dates:
        valid = active = 0
        for code in codes:
            v = cache.get(code, {}).get(d)
            if v is None:
                continue
            valid += 1
            if v > ACTIVE_THRESHOLD_PCT:
                active += 1
        ok = valid >= need
        series.append({'date': d,
                       'active_ratio_pct': round(active / valid * 100, 1) if ok else None,
                       'valid_n': valid})
    return series


def stock_trend_stats(code, trading_dates):
    """个股换手率趋势统计: 今日/上一交易日/近5/10/20日均值

    v4: 均线样本覆盖率不足时返回 None（页面显示「—」），不拿 1-2 个点冒充 20 日均值。
    """
    s = dict(_stock_cache.get(code, {}))
    today_v = s.get(trading_dates[-1])
    prev_d = trading_dates[-2] if len(trading_dates) >= 2 else None

    def avg(k):
        win = trading_dates[-k:]
        v = [s[d] for d in win if d in s]
        if len(v) < max(2, int(len(win) * MIN_AVG_COVERAGE)):
            return None
        return round(sum(v) / len(v), 2)

    if today_v is None and prev_d is None and avg(5) is None:
        return None
    return {
        'today': today_v,
        'prev_day': s.get(prev_d) if prev_d else None,
        'avg_5d': avg(5), 'avg_10d': avg(10), 'avg_20d': avg(20),
    }


def build_sector_block(name, scan_res, trading_dates):
    """单个板块完整数据块: 今日值 + 历史序列 + top5/bottom5个股趋势"""
    codes = scan_res['codes']
    today_iso = trading_dates[-1]
    # 确保所有成分股历史在缓存中（增量 backfill）
    need = [c for c in codes if not (today_iso in _stock_cache.get(c, {})
            and len([d for d in _stock_cache.get(c, {}) if d >= trading_dates[0]]) >= HIST_TRADING_DAYS - 2)]
    start_iso = trading_dates[0]
    if need:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(lambda c: backfill_stock(c, start_iso), need))

    series = sector_active_series(codes, trading_dates)

    def window_stats(series_list):
        vals = [p['active_ratio_pct'] for p in series_list]

        def avg(k):
            win = vals[-k:]
            v = [x for x in win if x is not None]
            if len(v) < max(2, int(len(win) * MIN_AVG_COVERAGE)):
                return None
            return round(sum(v) / len(v), 1)

        return {'today': vals[-1] if vals else None,
                'prev_day': vals[-2] if len(vals) >= 2 else None,
                'avg_5d': avg(5), 'avg_10d': avg(10), 'avg_20d': avg(20)}

    stats = window_stats(series)

    # top5/bottom5 个股（按今日换手率）+ 趋势统计
    def with_trend(rows):
        out = []
        for r in rows:
            st = stock_trend_stats(r['代码'], trading_dates) or {}
            out.append({**r, **st})
        return out

    delta = round(stats['today'] - stats['prev_day'], 1) if (stats['today'] is not None and stats['prev_day'] is not None) else None
    trend_icon = '📈' if (delta is not None and delta > 2) else ('📉' if (delta is not None and delta < -2) else '→')

    return {
        '板块': name,
        '成分股数': scan_res['n_stocks'],
        '可交易股数': scan_res['n_tradable'],
        '剔除B股': scan_res.get('excluded_b', 0),
        '剔除停牌': scan_res.get('excluded_susp', 0),
        '量化活跃股数': scan_res['active_count'],
        '量化集中度%_今日': stats['today'],
        '量化集中度%_上一交易日': stats['prev_day'],
        '环比变化pct': delta,
        '近5日均值%': stats['avg_5d'],
        '近10日均值%': stats['avg_10d'],
        '近20日均值%': stats['avg_20d'],
        '趋势': trend_icon,
        '历史序列': series,          # [{date, active_ratio_pct, valid_n}] × 21
        'top5_stocks': with_trend(scan_res['top5_today']),
        'bottom5_stocks': with_trend(scan_res['bottom5_today']),
    }


# ---------------- ETF（AI相关 + 宽基国家队代理） ----------------
def collect_etfs(trading_dates):
    etf_spot = _retry(ak.fund_etf_spot_em)
    out = {'ai_union_count': None, 'ai_union_amt_yi': None,
           'broad_today_yi': None, 'broad_series': []}

    # AI相关 ETF union（今日）
    all_kw = list({k for ks in SECTOR_ETF_KEYWORDS.values() for k in ks})
    mask_ai = etf_spot['名称'].str.contains('|'.join(all_kw), na=False)
    ai_union = etf_spot[mask_ai].drop_duplicates(subset='代码')
    out['ai_union_count'] = int(len(ai_union))
    out['ai_union_amt_yi'] = round(float(pd.to_numeric(ai_union.get('成交额'), errors='coerce').fillna(0).sum()) / 1e8, 2)

    # AI板块各自的 ETF（页面模块①展示用）
    per_sector = {}
    for sec, kws in SECTOR_ETF_KEYWORDS.items():
        m = etf_spot['名称'].str.contains('|'.join(kws), na=False)
        sub = etf_spot[m].drop_duplicates(subset='代码')
        per_sector[sec] = {'etf_count': int(len(sub)),
                           'etf_amt_yi': round(float(pd.to_numeric(sub.get('成交额'), errors='coerce').fillna(0).sum()) / 1e8, 2)}
    out['per_ai_sector'] = per_sector

    # 宽基 ETF（国家队代理）: 今日 + 历史序列
    mask_broad = etf_spot['名称'].str.contains('|'.join(BROAD_ETF_KEYWORDS), na=False)
    broad = etf_spot[mask_broad].drop_duplicates(subset='代码')
    out['broad_count'] = int(len(broad))
    amt_today = float(pd.to_numeric(broad.get('成交额'), errors='coerce').fillna(0).sum()) / 1e8
    out['broad_today_yi'] = round(amt_today, 2)

    # 宽基历史（逐ETF取fund_etf_hist_em后按日求和）— 限流保护: 只取成交额Top40
    broad_top = broad.nlargest(min(40, len(broad)), '成交额') if len(broad) else broad
    daily_amt = {}
    codes_list = [str(c) for c in broad_top['代码'].tolist()]

    def one(code):
        try:
            df = _retry(ak.fund_etf_hist_em, symbol=code, period="daily",
                        start_date=trading_dates[0].replace('-', ''), end_date=date.today().strftime('%Y%m%d'))
            if df is not None and len(df) > 0:
                for _, r in df.iterrows():
                    d = str(r['日期'])[:10]
                    daily_amt[d] = daily_amt.get(d, 0.0) + float(_safe_float(r.get('成交额')))
        except Exception:  # noqa: BLE001
            pass

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        list(ex.map(one, codes_list))
    for d in trading_dates:
        out['broad_series'].append({'date': d, 'amt_yi': round(daily_amt.get(d, 0.0) / 1e8, 2)})
    print(f"✅ ETF: AI相关union {out['ai_union_count']}只/{out['ai_union_amt_yi']}亿 | "
          f"宽基{out['broad_count']}只 今日{out['broad_today_yi']}亿 (历史Top40合计)")
    return out


# ---------------- 主力资金 + 北向 ----------------
def collect_flows():
    out = {'main_flow_series': [], 'northbound': None}
    try:
        df = _retry(ak.stock_market_fund_flow)
        tail = df.tail(HIST_TRADING_DAYS)
        for _, r in tail.iterrows():
            out['main_flow_series'].append({
                'date': str(r['日期'])[:10],
                'net_yi': round(_safe_float(r.get('主力净流入-净额')) / 1e8, 2),
                'pct': _safe_float(r.get('主力净流入-净占比')),
            })
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 主力资金流获取失败: {e}")
    try:
        df_nb = _retry(ak.stock_hsgt_fund_flow_summary_em)
        nb_rows = df_nb[df_nb['资金方向'] == '北向'] if len(df_nb) else None
        if nb_rows is not None and len(nb_rows):
            netbuy = sum(_safe_float(x) for x in nb_rows['成交净买额']) / 1e8
            out['northbound'] = {'date': str(nb_rows.iloc[0]['交易日'])[:10],
                                 'netbuy_yi': round(netbuy, 2),
                                 'note': '北向实时净买额（披露规则调整后部分日期为0）'}
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 北向资金获取失败: {e}")
    if out['main_flow_series']:
        last = out['main_flow_series'][-1]
        print(f"✅ 主力资金: 今日{last['net_yi']}亿 | 北向净买额: "
              f"{out['northbound']['netbuy_yi'] if out['northbound'] else '—'}亿")
    return out


# ---------------- 今日财经要闻（复用 Sina News digest） ----------------
def dedupe_tiers(scan):
    """同族层级去重: 成分股集合完全相同的板块只保留最短名（基础层）"""
    groups = {}
    for name, res in scan.items():
        key = frozenset(res['codes'])
        groups.setdefault(key, []).append(name)
    keep = set()
    dup_count = 0
    for names in groups.values():
        if len(names) == 1:
            keep.add(names[0])
        else:
            rep = min(names, key=len)
            keep.add(rep)
            dup_count += 1
            print(f"   🧹 层级去重 {names} → {rep}")
    if dup_count:
        print(f"   (共去除{dup_count}组同族重复板块)")
    return {n: r for n, r in scan.items() if n in keep}


def split_universe(scan, min_n=MIN_SECTOR_STOCKS):
    """把板块池拆成『互不包含的行业』与『细分叶子板块』两组

    v4 核心修正：东财 496 个行业板块是**嵌套层级**（印制电路板 ⊂ 元件 ⊂ 电子、
    营销代理 ⊂ 广告营销 ⊂ 传媒、证券Ⅱ ⊂ 非银金融 …）。`frozenset` 完全相等才合并的
    去重挡不住真子集 —— 实测 180 个候选里 149 个是别的板块的子集，Top10 有 9/10 是子板块，
    排名反映的是「切得够不够细」而不是「哪个行业量化最集中」。

    返回 (independent, leaves, dropped)：dropped[a] = 直接父板块名。
    """
    elig = {n: r for n, r in scan.items() if r['n_stocks'] >= min_n}
    sets = {n: set(r['codes']) for n, r in elig.items()}
    independent, dropped = [], {}
    for a in sets:
        parent = next((b for b in sets if b != a and sets[a] < sets[b]), None)
        if parent:
            dropped[a] = parent
        else:
            independent.append(a)
    leaves = [n for n in sets if n not in independent]
    print(f"🧹 真子集剔除: {len(dropped)} 个板块是别的板块的子集 | 独立行业 {len(independent)} | 细分叶子 {len(leaves)}")
    return independent, leaves, dropped


def collect_news_digest():
    """读取当日 digest —— v4: 按主题优先级合并多个文件

    此前用 `sorted(..., reverse=True)[0]`，字典序最后的是 `*_Unclassified.md`，
    于是 Finance/Market/Policy 的 🧠 AI 归纳一条都没读到，页面永远显示「未分类 + 中东地缘」。
    """
    result = {'date': None, 'themes_points': [], 'items': [], 'source_files': []}
    if not os.path.isdir(VAULT_DIGEST_DIR):
        print("⚠️ 未找到 digest 目录, 模块③显示占位")
        return result
    today_iso = date.today().strftime('%Y%m%d')
    all_files = [f for f in os.listdir(VAULT_DIGEST_DIR) if re.match(r'^\d{8}_.+\.md$', f)]
    todays = {f[9:].rsplit('.md', 1)[0]: f for f in all_files if f.startswith(today_iso)}
    if not todays:
        latest = sorted(all_files, reverse=True)[:1]
        if not latest:
            return result
        todays = {latest[0][9:].rsplit('.md', 1)[0]: latest[0]}

    picked = [todays[t] for t in NEWS_PRIORITY if t in todays]
    if not picked:                       # 当日只有未分类时才兜底
        picked = [todays[t] for t in todays]
    result['source_files'] = picked
    result['date'] = picked[0][:8]

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
            line = lines[i]
            hm = re.match(r'^#{2,4}\s+(.+?)\s*(?:\(\d+\s*条\))?\s*$', line)
            if hm:
                cur_theme = hm.group(1).strip()
                i += 1
                continue
            if '🧠 AI 归纳要点' in line:
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
            im = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', line)
            if im and len(result['items']) < 12 and im.group(2) not in seen_urls:
                seen_urls.add(im.group(2))
                result['items'].append({'title': im.group(1).strip()[:80], 'url': im.group(2)})
            i += 1

    print(f"✅ 财经要闻: {len(picked)}个主题文件 {picked} | "
          f"AI要点{len(result['themes_points'])}条, 条目{len(result['items'])}条")
    return result


# ---------------- 投资信号（阈值判定） ----------------
def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return round(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, 1)


def _pct_rank(hist, value):
    """value 在 hist 中的百分位（0-100），用于替代拍脑袋的绝对阈值"""
    h = [x for x in hist if x is not None]
    if not h or value is None:
        return None
    return round(sum(1 for x in h if x < value) / len(h) * 100)


def _quantile(hist, q):
    h = sorted(x for x in hist if x is not None)
    if len(h) < 5:
        return None
    i = min(len(h) - 1, max(0, int(round(q * (len(h) - 1)))))
    return round(h[i], 1)


def _zscore(hist, value):
    h = [x for x in hist if x is not None]
    if len(h) < 5 or value is None:
        return None
    mu = sum(h) / len(h)
    sd = (sum((x - mu) ** 2 for x in h) / len(h)) ** 0.5
    return round((value - mu) / sd, 2) if sd else None


def evaluate_signals(etf_data, flows, ai_blocks):
    """v4: 阈值全部改滚动分位/z值

    旧版是绝对值阈值（30亿 / ±30亿 / 35%），对 A 股是常数：实测 21 个交易日
    触发 21/21、20/21、21/21，面板天天「3/4 已触发」，等于没有信息。
    另：ETF 成交额含申购+赎回双边，措辞不再写「流入」。
    """
    sigs = []

    def add(name, current, unit, triggered, *, percentile=None, zscore=None,
            baseline='', direction='', note=''):
        sigs.append({'name': name, 'current': current, 'unit': unit,
                     'triggered': bool(triggered), 'percentile': percentile, 'zscore': zscore,
                     'baseline': baseline, 'threshold': baseline,
                     'direction': direction if triggered else '', 'note': note})

    # ① 宽基ETF成交额（国家队代理）：今日 vs 前 20 个交易日的 80 分位
    series = etf_data.get('broad_series') or []
    hist = [p.get('amt_yi') for p in series[:-1]]
    today = etf_data.get('broad_today_yi')
    p_etf = _pct_rank(hist, today)
    p80 = _quantile(hist, 0.8)
    add('宽基ETF成交额(国家队代理)', today, '亿',
        (p_etf is not None and p_etf >= 80),
        percentile=p_etf,
        baseline=f'前{len([h for h in hist if h])}日80分位 {p80}亿' if p80 else '历史不足',
        direction=f'宽基ETF成交处于近{len(hist)}日高位（P{p_etf}）' if p_etf is not None and p_etf >= 80 else '',
        note='成交额含申赎双边，不等于净流入')

    # ② 主力净流入：20日 z 分数 |z| ≥ 1
    mf = [x.get('net_yi') for x in (flows.get('main_flow_series') or [])]
    if mf:
        today_mf, hist_mf = mf[-1], mf[:-1]
        z = _zscore(hist_mf, today_mf)
        mu = round(sum(h for h in hist_mf if h is not None) / max(1, len([h for h in hist_mf if h is not None])), 1)
        add('主力净流入', today_mf, '亿', (z is not None and abs(z) >= 1.0),
            percentile=_pct_rank(hist_mf, today_mf), zscore=z,
            baseline=f'近{len(hist_mf)}日均值 {mu}亿',
            direction=('主力资金异动加仓' if (today_mf or 0) > 0 else '主力资金异动撤离')
                      if (z is not None and abs(z) >= 1.0) else '',
            note='z 分数 |z|≥1 视为异动')
    else:
        add('主力净流入', None, '亿', False, note='数据缺失')

    # ③ 北向：已停止披露实时净买额 → 永久下线，不再占信号位、不再计入分母
    sigs.append({'name': '北向单日净买额', 'current': None, 'unit': '亿', 'triggered': False,
                 'percentile': None, 'zscore': None, 'baseline': '停止披露',
                 'threshold': '停止披露', 'direction': '',
                 'note': '2024-08 起不再披露实时净买额，本信号停用'})

    # ④ AI板块活跃度：4 板块**中位数** vs 自身历史中位数序列的 80 分位
    #    （旧版取 max —— 4 个热门板块的最大值永远在高位，必然天天触发）
    panels = [b for b in ai_blocks if b.get('量化集中度%_今日') is not None]
    if panels:
        med = _median([b['量化集中度%_今日'] for b in panels])
        hist_med = []
        for idx in range(len(panels[0]['历史序列'])):
            vals = [b['历史序列'][idx]['active_ratio_pct'] for b in panels]
            if all(v is not None for v in vals):
                hist_med.append(_median(vals))
        p_ai = _pct_rank(hist_med[:-1], med)
        hot = max(panels, key=lambda b: b['量化集中度%_今日'])
        add('AI板块活跃度(4板块中位数)', med, '%', (p_ai is not None and p_ai >= 80),
            percentile=p_ai,
            baseline=f'近{len(hist_med) - 1}日中位数基线',
            direction=f'AI板块活跃度中位数高位（P{p_ai}，{hot["板块"]}最高 {hot["量化集中度%_今日"]}%）'
                      if p_ai is not None and p_ai >= 80 else '',
            note='中位数口径；最大值口径会恒真')
    else:
        add('AI板块活跃度(4板块中位数)', None, '%', False, note='数据缺失')

    triggered = [s for s in sigs if s['triggered']]
    print(f"✅ 投资信号: {len(triggered)}/{len(sigs)} 触发 → " + "; ".join(s['name'] for s in triggered))
    return {'rules': {'broad_etf': '近20日80分位', 'main_flow': '|z|≥1', 'ai_activity': '中位数近20日80分位'},
            'signals': sigs, 'triggered_count': len(triggered),
            'push_line': ' | '.join(f"📢 {s['direction'] or s['name']} (当前{s['current']}{s['unit']})"
                                    for s in triggered)}


# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    print("=== quant-ratio v4: 四模块数据收集 ===\n")

    today_iso = os.environ.get('QUANT_FORCE_DATE') or date.today().isoformat()  # 支持回归测试非交易日路径
    load_cache()

    # [0] 交易日闸门：非交易日直接退出（不写缓存、不落归档、不推信号）
    trading_dates, is_trading_day = get_trading_dates(today_iso)
    if not trading_dates:
        print("❌ 无法获取交易日历，终止（避免写入脏数据）")
        sys.exit(1)
    if not is_trading_day:
        print(f"⏸ {today_iso} 非交易日（最近交易日 {trading_dates[-1]}），跳过采集")
        return
    print(f"→ 交易日窗口: {trading_dates[0]} → {trading_dates[-1]} ({len(trading_dates)}个交易日)")

    # [1] 全市场扫描 + 精确同名档去重
    scan = scan_all_sectors(today_iso)
    scanned_n = len(scan)
    scan = dedupe_tiers(scan)
    exact_n = len(scan)

    # [2] 拆分「互不包含行业」/「细分叶子」—— v4 核心修正
    independent, leaves, dropped = split_universe(scan)
    eligible_n = len(independent) + len(leaves)

    def _rank(names):
        return sorted(((n, scan[n]) for n in names),
                      key=lambda kv: (kv[1]['active_ratio_pct'] or 0), reverse=True)

    rank_ind = _rank(independent)
    rank_leaf = _rank(leaves)
    top10_names = [n for n, _ in rank_ind[:10]]
    bottom10_names = [n for n, _ in rank_ind[-10:]]
    leaf_top10 = [n for n, _ in rank_leaf[:10]]
    display_names = list(dict.fromkeys(AI_SECTORS + top10_names + bottom10_names + leaf_top10))
    print(f"→ 排名池: 扫描{scanned_n} → 精确去重{exact_n} → 候选{eligible_n} → "
          f"独立行业{len(independent)} / 叶子{len(leaves)}")
    print(f"→ 行业Top10: {top10_names}")
    print(f"→ 行业Bottom10: {bottom10_names}")

    # [3] ETF + 资金流
    etf_data = collect_etfs(trading_dates)
    flows = collect_flows()

    # [4] 展示板块完整数据块（含历史序列+个股趋势）— 逐个构建(内部有并发backfill)
    ai_blocks, top10_blocks, bottom10_blocks, leaf_blocks = [], [], [], []
    for name in display_names:
        if name not in scan:
            print(f"   ⚠️ {name} 无成分股数据, 跳过")
            continue
        blk = build_sector_block(name, scan[name], trading_dates)
        if name in SECTOR_ETF_KEYWORDS and etf_data.get('per_ai_sector', {}).get(name):
            blk['相关ETF数'] = etf_data['per_ai_sector'][name]['etf_count']
            blk['相关ETF成交亿'] = etf_data['per_ai_sector'][name]['etf_amt_yi']
        if name in AI_SECTORS:
            ai_blocks.append(blk)
        if name in top10_names:               # AI 板块若同时进行业Top10，两个页签都要有
            top10_blocks.append(blk)
        elif name in bottom10_names:
            bottom10_blocks.append(blk)
        elif name not in AI_SECTORS:
            leaf_blocks.append(blk)
        print(f"   ✅ {name}: 今日{blk['量化集中度%_今日']}% (5日均{blk['近5日均值%']}%) "
              f"{blk['趋势']} | 成分股{blk['成分股数']}只(可交易{blk['可交易股数']})")

    # [5] 要闻 + 信号
    news = collect_news_digest()
    signals = evaluate_signals(etf_data, flows, ai_blocks)

    # [6] 全量排名（第三方可复核）+ 数据质量
    def _rank_row(n):
        r = scan[n]
        return {'板块': n, '成分股数': r['n_stocks'], '可交易股数': r['n_tradable'],
                '量化活跃股数': r['active_count'], '集中度%': r['active_ratio_pct'],
                'codes': r['codes']}

    ranking_all = [_rank_row(n) for n, _ in rank_ind]
    b_uniq, s_uniq = set(), set()
    for r in scan.values():
        b_uniq.update(r.get('excluded_b_codes') or [])
        s_uniq.update(r.get('excluded_susp_codes') or [])
    data_quality = {
        'b_share_excluded': len(b_uniq),          # 去重后的股票只数
        'suspended_excluded': len(s_uniq),
        'b_share_occurrences': sum(r.get('excluded_b', 0) for r in scan.values()),
        'suspended_occurrences': sum(r.get('excluded_susp', 0) for r in scan.values()),
        'boards_with_exclusions': sum(1 for r in scan.values()
                                      if r.get('excluded_b') or r.get('excluded_susp')),
    }

    data = {
        'version': 4,
        'date': today_iso,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'active_threshold_pct': ACTIVE_THRESHOLD_PCT,
        'min_sector_stocks': MIN_SECTOR_STOCKS,
        'trading_dates_window': [trading_dates[0], trading_dates[-1]],
        'universe': {'scanned': scanned_n, 'exact_deduped': exact_n,
                     'eligible': eligible_n, 'independent': len(independent),
                     'leaves': len(leaves)},
        'sector_universe_size': exact_n,
        'ranking_all': ranking_all,
        'excluded_nested': dropped,
        'data_quality': data_quality,
        'ai_sectors': ai_blocks,
        'top10_sectors': top10_blocks,
        'bottom10_sectors': bottom10_blocks,
        'leaf_sectors': leaf_blocks,
        'etf': etf_data,
        'flows': flows,
        'news_digest': news,
        'signals': signals,
    }

    save_cache()
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"✅ 数据已保存至 {DATA_FILE}")

    os.makedirs(HISTORY_DIR, exist_ok=True)
    theme = sys.argv[1] if len(sys.argv) > 1 else "quant"
    hist_path = os.path.join(HISTORY_DIR, f"{theme}_{date.today().strftime('%Y-%m-%d')}.json")
    with open(hist_path, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"✅ 历史归档已保存至 {hist_path}")

    # 供 run 脚本/cron 判断是否推送
    with open(os.path.join(DASHBOARD_PATH, "quant_push_signal.txt"), 'w') as f:
        f.write(signals['push_line'] if signals['triggered_count'] else "(无触发)")
    if signals['triggered_count'] > 0:
        print(f"SIGNAL_PUSH: {signals['push_line']}")
    else:
        print("SIGNAL_PUSH: (无触发)")

    print(f"\n=== v4 收集完成, 耗时 {int(time.time()-t0)}s ===")


if __name__ == "__main__":
    main()
