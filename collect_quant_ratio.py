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
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import akshare as ak

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")
VAULT_DIGEST_DIR = os.path.expanduser("~/Documents/Obsidian Vault/raw/articles/groundapi")

BROAD_ETF_KEYWORDS = ["沪深300", "上证50", "中证500"]
AI_ETF_KEYWORDS = ['人工智能', 'AI', '算力', '大数据', '云计算', '软件', '芯片', '半导体', '通信', '5G']
HIST_TRADING_DAYS = 21
MAX_WORKERS = 6
NEWS_PRIORITY = ['Finance', 'Market', 'Policy', 'AI', 'International', 'Business', 'DeepTech']


def _safe_float(x, default=None):
    try:
        v = float(x)
        return v if v == v else default      # NaN → default
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
                r = sess.get(url, params=params, timeout=20, headers=headers)
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
    try:
        spot = _retry(ak.fund_etf_spot_em, attempts=2)
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ fund_etf_spot_em 分页失败，启用逐页容错重试: {e}")
        spot = _fetch_etf_spot_resilient()
    spot['代码'] = spot['代码'].astype(str)
    names = dict(zip(spot['代码'], spot['名称'].astype(str)))
    price = dict(zip(spot['代码'], spot['最新价'].apply(_safe_float)))
    shares_today = dict(zip(spot['代码'], spot['最新份额'].apply(_safe_float)))
    amt_today = dict(zip(spot['代码'], spot['成交额'].apply(_safe_float)))

    broad = [c for c, n in names.items() if any(k in n for k in BROAD_ETF_KEYWORDS)]
    sh_broad = [c for c in broad if c.startswith('5')]
    sz_broad = [c for c in broad if not c.startswith('5')]
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
            netbuy.append({'date': d, 'net_yi': None, 'sh_yi': None, 'sz_yi': None})
        else:
            total = round((sh_amt or 0) + (sz_amt or 0), 2)
            netbuy.append({'date': d, 'net_yi': total, 'sh_yi': sh_amt, 'sz_yi': sz_amt})
    out['netbuy_series'] = netbuy
    out['netbuy_today_yi'] = next((p['net_yi'] for p in netbuy if p['date'] == today_iso), None)
    today_entry = next((p for p in netbuy if p['date'] == today_iso), None)

    # 覆盖率 = 今日能算出「变化」的宽基份额 / 全市场宽基份额
    # 深市要等到有前一日快照才能算变化，所以首日覆盖率=沪市占比（~85%），次日起 ~100%
    tot = sum(shares_today.get(c) or 0 for c in broad)
    cov_sh = sum(sh_snap.get(c) or 0 for c in sh_broad)
    sz_countable = today_entry is not None and today_entry.get('sz_yi') is not None
    cov_sz = sum((sz_cache.get(today_iso) or {}).get(c) or 0 for c in sz_broad) if sz_countable else 0
    out['netbuy_coverage_pct'] = round((cov_sh + cov_sz) / tot * 100, 1) if tot else None
    out['sz_delta_available'] = sz_countable
    out['netbuy_basis'] = (
        f"宽基ETF份额净申购 = 沪市（交易所按日，{len(sh_broad)}只，可回溯）+ "
        f"深市（交易所快照逐日累积，{len(sz_broad)}只，自 {out['sz_tracked_since'] or today_iso} 起，"
        f"已 {out['sz_tracked_days']} 个快照日）× 最新价；"
        f"当前覆盖 {out['netbuy_coverage_pct']}% 宽基份额"
        + ('' if sz_countable else '（深市自次日起可算变化）'))

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
def collect_flows():
    out = {'main_flow_series': [], 'northbound': None}
    try:
        df = _retry(ak.stock_market_fund_flow)
        for _, r in df.tail(HIST_TRADING_DAYS).iterrows():
            out['main_flow_series'].append({
                'date': str(r['日期'])[:10],
                'net_yi': round(_safe_float(r.get('主力净流入-净额'), 0) / 1e8, 2),
                'pct': _safe_float(r.get('主力净流入-净占比'), 0),
            })
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 主力资金流获取失败: {e}")
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
            baseline='', direction='', note=''):
        sigs.append({'name': name, 'current': current, 'unit': unit, 'triggered': bool(triggered),
                     'percentile': percentile, 'zscore': zscore, 'baseline': baseline,
                     'threshold': baseline, 'direction': direction if triggered else '', 'note': note})

    series = etf.get('netbuy_series') or []
    hist = [p['net_yi'] for p in series[:-1]]
    today = etf.get('netbuy_today_yi')
    p = _pct_rank(hist, today)
    p80 = _quantile(hist, 0.8)
    add('国家队ETF净申购(沪市宽基)', today, '亿', (p is not None and p >= 80),
        percentile=p,
        baseline=f'近{len([h for h in hist if h is not None])}日80分位 {p80}亿' if p80 is not None else '历史不足',
        direction=f'国家队大额申购（P{p}，{today}亿）' if (p is not None and p >= 80) else '',
        note='份额日变化×价格；ETF成交额含二级换手，不是净申购')

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
        add('主力净流入', None, '亿', False, note='数据缺失')

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

    etf = collect_etf(trading_dates, today_iso)
    flows = collect_flows()
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
