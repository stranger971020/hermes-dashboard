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
        codes = [str(c) for c in df_cons['代码'].tolist()]
        active = int(((to > ACTIVE_THRESHOLD_PCT).fillna(False)).sum())
        n = len(df_cons)
        # 今日值写入缓存（免费, 来自 cons 接口）
        pairs = []
        for i, code in enumerate(codes):
            t = to.iloc[i] if i < len(to) else None
            try:
                tv = float(t)
            except (TypeError, ValueError):
                continue
            if pd.notna(tv):
                pairs.append((code, round(float(tv), 3)))
        ranked = sorted(enumerate(codes), key=lambda x: (to.iloc[x[0]] if pd.notna(to.iloc[x[0]]) else -1), reverse=True)
        top5 = [{'代码': codes[i], '名称': str(df_cons['名称'].iloc[i]), '换手率%': round(float(to.iloc[i]), 2)}
                for i, _ in ranked[:5] if pd.notna(to.iloc[i])]
        bottom5 = [{'代码': codes[i], '名称': str(df_cons['名称'].iloc[i]), '换手率%': round(float(to.iloc[i]), 2)}
                   for i, _ in reversed(ranked[-5:]) if pd.notna(to.iloc[i])]
        return name, {
            'codes': codes,
            'n_stocks': n,
            'active_count': active,
            'active_ratio_pct': round(active / n * 100, 1) if n else None,
            'top5_today': top5[:5],
            'bottom5_today': bottom5[:5],
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
def get_trading_dates():
    """从主力资金流接口取交易日轴（真实交易日历, 含今日）"""
    try:
        df = _retry(ak.stock_market_fund_flow)
        dates = sorted(str(d)[:10] for d in df['日期'].tolist())[-HIST_TRADING_DAYS:]
        if len(dates) >= HIST_TRADING_DAYS - 2 and date.today().isoformat() not in dates:
            dates.append(date.today().isoformat())
        return dates
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 交易日轴获取失败({e}), 回退日历日")
        out = []
        d = date.today()
        while len(out) < HIST_TRADING_DAYS and d > date.today() - timedelta(days=45):
            if d.weekday() < 5:
                out.append(d.isoformat())
            d -= timedelta(days=1)
        return sorted(out)


def sector_active_series(codes, trading_dates):
    """逐日计算板块量化集中度(活跃占比%) — 分母=当日有数据的成分股"""
    series = []
    for d in trading_dates:
        valid = active = 0
        for code in codes:
            with _cache_lock:
                s = _stock_cache.get(code, {})
            if d in s:
                valid += 1
                if s[d] > ACTIVE_THRESHOLD_PCT:
                    active += 1
        series.append({'date': d, 'active_ratio_pct': round(active / valid * 100, 1) if valid >= 5 else None})
    return series


def stock_trend_stats(code, trading_dates):
    """个股换手率趋势统计: 今日/上一交易日/近5/10/20日均值"""
    with _cache_lock:
        s = dict(_stock_cache.get(code, {}))
    vals = [(d, s[d]) for d in sorted(s) if d in set(trading_dates)]
    if not vals:
        return None
    today_v = s.get(trading_dates[-1])
    prev_d = trading_dates[-2] if len(trading_dates) >= 2 else None
    def avg(k):
        v = [s[d] for d in trading_dates[-k:] if d in s]
        return round(sum(v) / len(v), 2) if v else None
    return {
        'today': today_v,
        'prev_day': s.get(prev_d) if prev_d else None,
        'avg_5d': avg(5), 'avg_10d': avg(10), 'avg_20d': avg(min(20, len(trading_dates))),
    }


def build_sector_block(name, scan_res, trading_dates):
    """单个板块完整数据块: 今日值 + 历史序列 + top5/bottom5个股趋势"""
    codes = scan_res['codes']
    # 确保所有成分股历史在缓存中（增量 backfill）
    need = [c for c in codes if not (date.today().isoformat() in _stock_cache.get(c, {})
            and len([d for d in _stock_cache.get(c, {}) if d >= trading_dates[0]]) >= HIST_TRADING_DAYS - 2)]
    start_iso = trading_dates[0]
    if need:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(lambda c: backfill_stock(c, start_iso), need))

    series = sector_active_series(codes, trading_dates)
    def window_stats(series_list):
        pts = [p['active_ratio_pct'] for p in series_list if p['active_ratio_pct'] is not None]
        def avg(k):
            v = [p['active_ratio_pct'] for p in reversed(series_list) if p['active_ratio_pct'] is not None][:k]
            return round(sum(v) / len(v), 1) if v else None
        return {'today': pts[-1] if pts else None, 'prev_day': (pts[-2] if len(pts) >= 2 else None),
                'avg_5d': avg(5), 'avg_10d': avg(10), 'avg_20d': avg(min(20, len(series_list)))}

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
        '量化活跃股数': scan_res['active_count'],
        '量化集中度%_今日': stats['today'],
        '量化集中度%_上一交易日': stats['prev_day'],
        '环比变化pct': delta,
        '近5日均值%': stats['avg_5d'],
        '近10日均值%': stats['avg_10d'],
        '近20日均值%': stats['avg_20d'],
        '趋势': trend_icon,
        '历史序列': series,          # [{date, active_ratio_pct}] × 21
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


def collect_news_digest():
    result = {'date': None, 'themes_points': [], 'items': []}
    if not os.path.isdir(VAULT_DIGEST_DIR):
        print("⚠️ 未找到 digest 目录, 模块③显示占位")
        return result
    today_iso = date.today().strftime('%Y%m%d')
    files = sorted([f for f in os.listdir(VAULT_DIGEST_DIR) if re.match(r'^\d{8}_.+\.md$', f)], reverse=True)
    chosen = next((f for f in files if f.startswith(today_iso)), files[0] if files else None)
    if not chosen:
        return result
    path = os.path.join(VAULT_DIGEST_DIR, chosen)
    try:
        with open(path) as fh:
            text = fh.read()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ digest读取失败({e})")
        return result
    result['date'] = chosen[:8]

    lines = text.splitlines()
    i, cur_theme = 0, None
    seen_urls = set()
    while i < len(lines):
        line = lines[i]
        hm = re.match(r'^#{2,4}\s+(.+?)\s*(?:\(\d+\s*条\))?\s*$', line)
        if hm:
            cur_theme = hm.group(1).strip()
            i += 1
            continue
        if '🧠 AI 归纳要点' in line and cur_theme:      # **🧠 AI 归纳要点**: 后跟 - bullet 行（中间可能空行）
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":   # 跳过标记后的空行
                j += 1
            while j < len(lines):
                lm = re.match(r'^\s*[-•]\s+(.+?)\s*$', lines[j])
                if not lm:
                    break
                result['themes_points'].append({'theme': cur_theme, 'point': lm.group(1)[:200]})
                j += 1
            i = j
            continue
        im = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', line)   # markdown表格/链接行
        if im and len(result['items']) < 15 and im.group(2) not in seen_urls:
            seen_urls.add(im.group(2))
            result['items'].append({'title': im.group(1).strip()[:80], 'url': im.group(2)})
        i += 1
    print(f"✅ 财经要闻: {chosen} | AI要点{len(result['themes_points'])}条, 条目{len(result['items'])}条")
    return result


# ---------------- 投资信号（阈值判定） ----------------
def evaluate_signals(etf_data, flows, ai_blocks):
    sigs = []
    def add(name, current, threshold, unit, triggered, direction='', note=''):
        sigs.append({'name': name, 'current': current, 'threshold': threshold, 'unit': unit,
                     'triggered': bool(triggered), 'direction': direction, 'note': note})

    broad_today = etf_data.get('broad_today_yi')
    t = THRESHOLDS['broad_etf_daily_yi']
    add('宽基ETF单日成交额', broad_today, t, '亿', (broad_today or 0) >= t,
        direction='国家队资金大幅流入' if (broad_today or 0) >= t else '', note='沪深300/上证50/中证500 ETF合计(去重Top40口径)')

    mf = flows.get('main_flow_series', [])
    main_today = mf[-1]['net_yi'] if mf else None
    t2 = THRESHOLDS['main_flow_net_yi']
    if (main_today or 0) >= t2:
        add('主力净流入', main_today, f'≥+{t2}', '亿', True, direction='主力资金加仓')
    elif (main_today or 0) <= -t2:
        add('主力净流出', main_today, f'≤-{t2}', '亿', True, direction='主力资金撤离')
    else:
        add('主力资金净流向', main_today, f'|{t2}|', '亿', False, note=f'今日 {main_today}亿 未达阈值')

    nb = flows.get('northbound') or {}
    t3 = THRESHOLDS['northbound_netbuy_yi']
    add('北向单日净买额', nb.get('netbuy_yi'), f'|≥{t3}|', '亿', (abs(nb.get('netbuy_yi') or 0) >= t3), note=nb.get('note', ''))

    ratios = [b['量化集中度%_今日'] for b in ai_blocks if b.get('量化集中度%_今日') is not None]
    max_ratio = max(ratios) if ratios else None
    hot_sector = next((b['板块'] for b in ai_blocks if b.get('量化集中度%_今日') == max_ratio), '—')
    t4 = THRESHOLDS['ai_active_ratio_pct']
    add(f'AI板块量化活跃度', max_ratio, f'≥{t4}', '%', (max_ratio or 0) >= t4,
        direction=f'{hot_sector}板块活跃占比 {max_ratio}% ≥ {t4}%' if (max_ratio or 0) >= t4 else '', note='skill判定标准')

    triggered = [s for s in sigs if s['triggered']]
    print(f"✅ 投资信号: {len(triggered)}/{len(sigs)} 触发 → " + "; ".join(s['name'] for s in triggered))
    return {'thresholds': THRESHOLDS, 'signals': sigs, 'triggered_count': len(triggered),
            'push_line': ' | '.join(f"📢 {s['direction'] or s['name']} (当前{s['current']}{s['unit']})" for s in triggered)}


# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    print("=== quant-ratio v3: 四模块数据收集 ===\n")

    today_iso = date.today().isoformat()
    load_cache()

    # [1] 交易日轴 + 全市场扫描
    trading_dates = get_trading_dates()
    print(f"→ 交易日窗口: {trading_dates[0]} → {trading_dates[-1]} ({len(trading_dates)}个交易日)")
    scan = scan_all_sectors(today_iso)
    scan = dedupe_tiers(scan)   # 同族层级去重（银行/银行Ⅱ 等成分股相同的只留基础层）

    # [2] 排名（成分股≥MIN_SECTOR_STOCKS, 去重后板块池）
    eligible = {n: r for n, r in scan.items() if r['n_stocks'] >= MIN_SECTOR_STOCKS}
    ranked = sorted(eligible.items(), key=lambda kv: (kv[1]['active_ratio_pct'] or 0), reverse=True)
    top10_names = [n for n, _ in ranked[:10]]
    bottom10_names = [n for n, _ in ranked[-10:] if n not in top10_names]
    display_names = list(dict.fromkeys(AI_SECTORS + top10_names + bottom10_names))
    print(f"→ 排名池: {len(eligible)}个板块(≥{MIN_SECTOR_STOCKS}只) | Top10={top10_names}")

    # [3] ETF + 资金流（与板块历史并行不了就先串行, 均为真实接口）
    etf_data = collect_etfs(trading_dates)
    flows = collect_flows()

    # [4] 展示板块完整数据块（含历史序列+个股趋势）— 逐个构建(内部有并发backfill)
    ai_blocks, top10_blocks, bottom10_blocks = [], [], []
    for name in display_names:
        if name not in scan:
            print(f"   ⚠️ {name} 无成分股数据, 跳过")
            continue
        blk = build_sector_block(name, scan[name], trading_dates)
        # AI板块附加 ETF 信息
        if name in SECTOR_ETF_KEYWORDS and etf_data.get('per_ai_sector', {}).get(name):
            blk['相关ETF数'] = etf_data['per_ai_sector'][name]['etf_count']
            blk['相关ETF成交亿'] = etf_data['per_ai_sector'][name]['etf_amt_yi']
        if name in AI_SECTORS:
            ai_blocks.append(blk)
        elif name in top10_names:
            top10_blocks.append(blk)
        else:
            bottom10_blocks.append(blk)
        print(f"   ✅ {name}: 今日{blk['量化集中度%_今日']}% (5日均{blk['近5日均值%']}%) "
              f"{blk['趋势']} | 成分股{blk['成分股数']}只")

    # [5] 要闻 + 信号
    news = collect_news_digest()
    signals = evaluate_signals(etf_data, flows, ai_blocks)

    data = {
        'version': 3,
        'date': today_iso,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'active_threshold_pct': ACTIVE_THRESHOLD_PCT,
        'min_sector_stocks': MIN_SECTOR_STOCKS,
        'trading_dates_window': [trading_dates[0], trading_dates[-1]],
        'sector_universe_size': len(scan),
        'ai_sectors': ai_blocks,
        'top10_sectors': top10_blocks,
        'bottom10_sectors': bottom10_blocks,
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

    print(f"\n=== v3 收集完成, 耗时 {int(time.time()-t0)}s ===")


if __name__ == "__main__":
    main()
