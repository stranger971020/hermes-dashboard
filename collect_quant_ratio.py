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
def collect_etf(trading_dates, today_iso):
    out = {'broad_count': None, 'broad_today_yi': None,
           'netbuy_series': [], 'netbuy_today_yi': None,
           'netbuy_coverage_pct': None,
           'netbuy_basis': '沪市宽基ETF（5开头）份额日变化 × 最新价，覆盖全市场宽基份额 N%',
           'ai_union_count': None, 'ai_union_amt_yi': None}

    spot = _retry(ak.fund_etf_spot_em)
    spot['代码'] = spot['代码'].astype(str)
    names = dict(zip(spot['代码'], spot['名称'].astype(str)))
    price = dict(zip(spot['代码'], spot['最新价'].apply(_safe_float)))
    shares_today = dict(zip(spot['代码'], spot['最新份额'].apply(_safe_float)))
    amt_today = dict(zip(spot['代码'], spot['成交额'].apply(_safe_float)))

    broad = [c for c, n in names.items() if any(k in n for k in BROAD_ETF_KEYWORDS)]
    sh_broad = [c for c in broad if c.startswith('5')]
    out['broad_count'] = len(broad)
    out['broad_today_yi'] = round(sum(amt_today.get(c) or 0 for c in broad) / 1e8, 2)

    tot = sum(shares_today.get(c) or 0 for c in broad)
    tot_sh = sum(shares_today.get(c) or 0 for c in sh_broad)
    out['netbuy_coverage_pct'] = round(tot_sh / tot * 100, 1) if tot else None
    out['netbuy_basis'] = f"沪市宽基ETF（{len(sh_broad)}只）份额日变化 × 最新价，覆盖全市场宽基份额 {out['netbuy_coverage_pct']}%"

    def fetch_sh(d):
        try:
            df = _retry(ak.fund_etf_scale_sse, date=d.replace('-', ''))
            df.columns = ['序号', '代码', '简称', '类型', '统计日', '份额']
            return d, {str(c): _safe_float(v) for c, v in zip(df['代码'], df['份额'])}
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 份额 {d} 获取失败: {e}")
            return d, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        shares_by_date = {d: s for d, s in ex.map(fetch_sh, trading_dates) if s}

    netbuy = []
    for i, d in enumerate(trading_dates):
        cur = shares_by_date.get(d)
        prev = shares_by_date.get(trading_dates[i - 1]) if i > 0 else None
        if not cur or not prev:
            netbuy.append({'date': d, 'net_yi': None})
            continue
        amount = 0.0
        for c in sh_broad:
            s1, s0, px = cur.get(c), prev.get(c), price.get(c)
            if s1 is None or s0 is None or not px:
                continue
            amount += (s1 - s0) * px
        netbuy.append({'date': d, 'net_yi': round(amount / 1e8, 2)})
    out['netbuy_series'] = netbuy
    out['netbuy_today_yi'] = next((p['net_yi'] for p in netbuy if p['date'] == today_iso), None)

    ai = [c for c, n in names.items() if any(k in n for k in AI_ETF_KEYWORDS)]
    out['ai_union_count'] = len(ai)
    out['ai_union_amt_yi'] = round(sum(amt_today.get(c) or 0 for c in ai) / 1e8, 2)

    print(f"✅ ETF: 宽基{len(broad)}只 今日成交{out['broad_today_yi']}亿 | "
          f"沪市净申购 {out['netbuy_today_yi']}亿（覆盖{out['netbuy_coverage_pct']}%份额）| "
          f"AI相关{out['ai_union_count']}只/{out['ai_union_amt_yi']}亿")
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
