#!/usr/bin/env python3
"""quant-ratio AI相关板块量化集中度 — 数据收集脚本 v2

v2 变更（2026-11-09 审计后重写）:
1. 独立数据文件 quant_data.json（不再与监控管道共用 data.json，避免被覆盖）
2. 所有指标来自真实数据源，无拍脑袋公式
3. 新增板块5日历史趋势（stock_board_industry_hist_em）

指标口径（全部可自证）:
- 成分股数        = stock_board_industry_cons_em(板块) 行数
- 板块换手率%     = stock_board_industry_name_em / hist_em 当日值
- 量化活跃股占比% = 成分股中当日换手率>3%的个股比例（同一 cons 接口）
- 相关ETF数/成交  = fund_etf_spot_em 按关键词映射，代码去重
- 趋势            = 近3日均换手 vs 前3日均换手（hist_em 真实历史）
- 综合评分(0-100) = 40*min(1,活跃占比/35%) + 30*min(1,板块换手/5%)
                    + 20*min(1,ETF成交亿/20) + 10*min(1,ETF数/30)
  （公式透明，页面展示；输入全为真实值）
"""

import sys
sys.path.insert(0, "/Users/jren/.hermes/skills/quant-ratio/scripts")

import akshare as ak
import pandas as pd
from datetime import date, timedelta
import json
import os

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "quant_data.json")  # v2: 独立文件，不与监控管道冲突

# 板块 → ETF关键词映射（代码去重；口径在页面展示）
SECTOR_ETF_KEYWORDS = {
    '计算机': ['人工智能', 'AI', '算力', '大数据', '云计算', '软件'],
    '电子':   ['芯片'],
    '通信设备': ['通信', '5G'],
    '半导体':  ['半导体'],
}

ACTIVE_TURNOVER_THRESHOLD = 3.0  # %，成分股换手率超过此值视为"量化活跃"


def _safe_float(x, default=0.0):
    try:
        v = float(x)
        return v if pd.notna(v) else default
    except (TypeError, ValueError):
        return default


def get_sector_data(sector, etf_df):
    """收集单个板块的全部真实指标"""
    result = {'板块': sector}

    # 1. 成分股（含换手率/成交额）
    try:
        df_cons = ak.stock_board_industry_cons_em(symbol=sector)
    except Exception as e:
        print(f"  ⚠️ {sector}: cons_em 失败 ({e})")
        return None

    if len(df_cons) == 0:
        print(f"  ⚠️ {sector}: 无成分股数据")
        return None

    result['成分股数'] = int(len(df_cons))

    # 2. 量化活跃股占比（换手率 > 阈值）
    turnover_series = pd.to_numeric(df_cons.get('换手率'), errors='coerce').fillna(0)
    active_count = int((turnover_series > ACTIVE_TURNOVER_THRESHOLD).sum())
    result['量化活跃股数'] = active_count
    result['量化活跃占比%'] = round(active_count / len(df_cons) * 100, 1)

    # 3. 成分股平均换手率（真实值）
    result['成分股均换手率%'] = round(float(turnover_series.mean()), 2)

    # 4. Top/Bottom 个股（按当日换手率排序，全来自同一接口）
    df_sorted = df_cons.copy()
    df_sorted['_t'] = turnover_series
    top5 = df_sorted.nlargest(5, '_t')
    bottom5 = df_sorted.nsmallest(5, '_t')

    def _stock_rows(df_part):
        rows = []
        for _, r in df_part.iterrows():
            rows.append({
                '代码': str(r['代码']),
                '名称': str(r['名称']),
                '换手率%': round(_safe_float(r.get('换手率')), 2),
                '成交额亿': round(_safe_float(r.get('成交额')) / 1e8, 2),
            })
        return rows

    result['top5_stocks'] = _stock_rows(top5)
    result['bottom5_stocks'] = _stock_rows(bottom5)

    # 5. 相关ETF（关键词映射，代码去重）
    keywords = SECTOR_ETF_KEYWORDS.get(sector, [])
    if len(etf_df) > 0 and keywords:
        mask = etf_df['名称'].str.contains('|'.join(keywords), na=False)
        df_etf_sub = etf_df[mask]
        result['ETF数'] = int(df_etf_sub['代码'].nunique())
        result['ETF成交亿'] = round(float(pd.to_numeric(df_etf_sub.get('成交额'), errors='coerce').fillna(0).sum()) / 1e8, 2)
    else:
        result['ETF数'] = 0
        result['ETF成交亿'] = 0.0

    # 6. 板块当日换手率 + 5日历史（真实）
    try:
        end_d = date.today()
        start_d = end_d - timedelta(days=21)  # 覆盖约10个交易日
        df_hist = ak.stock_board_industry_hist_em(
            symbol=sector,
            start_date=start_d.strftime('%Y%m%d'),
            end_date=end_d.strftime('%Y%m%d'))
        if len(df_hist) >= 6:
            hist_rows = []
            for _, r in df_hist.tail(5).iterrows():
                hist_rows.append({
                    '日期': str(r['日期']),
                    '涨跌幅%': round(_safe_float(r.get('涨跌幅')), 2),
                    '换手率%': round(_safe_float(r.get('换手率')), 2),
                })
            result['近5日'] = hist_rows
            recent = [_safe_float(x) for x in df_hist['换手率'].tail(3)]
            prior = [_safe_float(x) for x in df_hist['换手率'].iloc[-6:-3]]
            r_avg, p_avg = sum(recent) / len(recent), sum(prior) / len(prior)
            result['板块换手率%'] = round(r_avg, 2)
            if p_avg > 0:
                change = (r_avg - p_avg) / p_avg * 100
                result['趋势'] = '📈' if change > 5 else ('📉' if change < -5 else '→')
                result['换手变化%'] = round(change, 1)
            else:
                result['趋势'] = '→'
                result['换手变化%'] = 0.0
        elif len(df_hist) > 0:
            result['近5日'] = [{
                '日期': str(r['日期']),
                '涨跌幅%': round(_safe_float(r.get('涨跌幅')), 2),
                '换手率%': round(_safe_float(r.get('换手率')), 2)} for _, r in df_hist.iterrows()]
            result['板块换手率%'] = round(float(pd.to_numeric(df_hist['换手率'], errors='coerce').fillna(0).mean()), 2)
            result['趋势'] = '→'
    except Exception as e:
        print(f"  ⚠️ {sector}: hist_em 失败 ({e})")

    # 7. 综合评分（透明公式）
    active_ratio = result.get('量化活跃占比%', 0) / 35.0
    sector_to = result.get('板块换手率%', 0) / 5.0
    etf_amt = result.get('ETF成交亿', 0) / 20.0
    etf_cnt = result.get('ETF数', 0) / 30.0
    score = (40 * min(1, active_ratio) + 30 * min(1, sector_to)
             + 20 * min(1, etf_amt) + 10 * min(1, etf_cnt))
    result['综合评分'] = round(score, 1)
    result['评级'] = '高' if score >= 60 else ('中' if score >= 40 else '低')

    return result


def get_ai_sector_data():
    """获取AI相关板块量化集中度数据（v2）"""
    print("=== 收集AI相关板块量化集中度数据 (v2) ===\n")

    # ETF 行情只取一次（全市场1666只）
    try:
        etf_df = ak.fund_etf_spot_em()
        print(f"✅ ETF行情: {len(etf_df)}只\n")
    except Exception as e:
        print(f"⚠️ ETF行情获取失败: {e}")
        etf_df = pd.DataFrame(columns=['代码', '名称', '成交额'])

    sectors = ['计算机', '电子', '通信设备', '半导体']
    sector_results = []
    for s in sectors:
        print(f"→ 收集板块: {s}")
        sd = get_sector_data(s, etf_df)
        if sd:
            print(f"   ✅ 成分股{sd['成分股数']}只 | 活跃占比{sd.get('量化活跃占比%',0)}% "
                  f"| ETF {sd.get('ETF数',0)}只/{sd.get('ETF成交亿',0)}亿 | 评分{sd['综合评分']}")
            sector_results.append(sd)

    if not sector_results:
        print("❌ 无板块数据收集成功")
        return None

    # 全市场汇总（真实值）
    all_etf_mask = etf_df['名称'].str.contains(
        '|'.join(k for ks in SECTOR_ETF_KEYWORDS.values() for k in ks), na=False) \
        if len(etf_df) > 0 else None
    ai_etf_union = int(all_etf_mask.sum()) if all_etf_mask is not None and any(all_etf_mask) else 0

    result = {
        'date': str(date.today()),
        'generated_at': date.today().strftime('%Y-%m-%d %H:%M'),
        'sectors': sector_results,
        # 汇总（供页面头部展示）
        'ai_etf_union_count': ai_etf_union,
        'active_threshold_pct': ACTIVE_TURNOVER_THRESHOLD,
    }

    print(f"\n✅ 收集完成: {len(sector_results)}个板块 | AI相关ETF(去重) {ai_etf_union}只")
    return result


def save_data(data):
    """保存数据到 quant_data.json（v2: 独立文件）"""
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 数据已保存至 {DATA_FILE}")


def save_history(data, theme="quant"):
    """保存历史归档（主题+日期命名）"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    filename = f"{theme}_{date.today().strftime('%Y-%m-%d')}.json"
    filepath = os.path.join(HISTORY_DIR, filename)
    with open(filepath, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 历史归档已保存至 {filepath}")


if __name__ == "__main__":
    ai_data = get_ai_sector_data()
    if ai_data:
        save_data(ai_data)
        theme = sys.argv[1] if len(sys.argv) > 1 else "quant"
        save_history(ai_data, theme=theme)
        print("\n=== quant-ratio 数据收集完成 (v2) ===")
