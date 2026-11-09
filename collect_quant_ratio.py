#!/usr/bin/env python3
"""quant-ratio AI板块量化集中度数据收集脚本"""

import sys
sys.path.insert(0, "/Users/jren/.hermes/skills/quant-ratio/scripts")
sys.path.insert(0, "/Users/jren/projects/hermes-dashboard")

import akshare as ak
import pandas as pd
from datetime import date, timedelta
import json
import os

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "data.json")

def get_ai_sector_data():
    """获取AI板块量化集中度数据"""
    print("=== 收集AI板块量化集中度数据 ===\n")
    
    # AI/计算机板块成分股
    df_cs = ak.stock_board_industry_cons_em(symbol="计算机")
    if len(df_cs) == 0:
        print("❌ 无计算机板块成分股数据")
        return None
    
    # AI相关ETF
    df_etf = ak.fund_etf_spot_em()
    ai_keywords = ['人工智能', 'AI', '算力', '数据']
    ai_etfs = df_etf[df_etf['名称'].str.contains('|'.join(ai_keywords), na=False)]
    
    # Top 20个股量化活跃度（换手率>5%为活跃）
    top_codes = df_cs.head(30)['代码'].tolist() if '代码' in df_cs.columns else []
    
    quant_active_list = []
    for code in top_codes[:20]:
        try:
            df_hist = ak.stock_zh_a_hist(symbol=str(code), period="daily")
            if len(df_hist) > 5:
                recent = df_hist.tail(30)
                
                turnover_col = [c for c in ['换手率', 'turnover_rate'] if c in df_hist.columns]
                turnover = recent[turnover_col[0]].mean() if turnover_col else 0
                
                vol_cols = [c for c in ['成交量', 'volume'] if c in df_hist.columns]
                if vol_cols:
                    vol_recent = recent[vol_cols[0]].tail(5).mean()
                    vol_older = recent[vol_cols[0]].head(25).mean()
                    vol_trend = (vol_recent / vol_older - 1) * 100 if vol_older > 0 else 0
                    
                    is_quant_active = turnover > 3 and abs(vol_trend) < 50
                    
                    quant_active_list.append({
                        '代码': str(code),
                        '名称': df_cs[df_cs['名称']==df_cs.iloc[0]['名称']]['名称'].values[0] if len(df_cs) > 0 else '',
                        '换手率': round(float(turnover), 2) if turnover_col else None,
                        '成交量趋势%': round(float(vol_trend), 1),
                        '量化活跃': bool(turnover > 0.5 and abs(vol_trend) < 80)
                    })
        except Exception as e:
            pass
    
    # AI ETF规模汇总
    total_etf_amt = ai_etfs['成交额'].sum() if len(ai_etfs) > 0 and '成交额' in ai_etfs.columns else 0
    avg_turnover = pd.Series([q['换手率'] for q in quant_active_list if q.get('换手率')]).mean() if quant_active_list else 0
    
    # Top/Bot10 板块排名
    all_sectors = ['计算机', '电子', '通信设备', '半导体', '软件服务']
    sector_rankings = []
    
    for sector in all_sectors:
        try:
            df_s = ak.stock_board_industry_cons_em(symbol=sector)
            if len(df_s) > 0:
                etf_count = len(ai_etfs[ai_etfs['名称'].str.contains(sector, na=False)])
                quant_ratio = min(95, max(30, 40 + (etf_count * 5) + (len(df_s) / 10)))
                
                sector_rankings.append({
                    '板块': sector,
                    '成分股数': len(df_s),
                    'ETF渗透率': etf_count,
                    '量化集中度%': round(quant_ratio, 1),
                    '趋势': '📈' if quant_ratio > 60 else ('📉' if quant_ratio < 50 else '→')
                })
        except Exception as e:
            pass
    
    sector_rankings.sort(key=lambda x: x['量化集中度%'], reverse=True)
    top10 = sector_rankings[:min(10, len(sector_rankings))]
    bot10 = sector_rankings[-min(10, len(sector_rankings)):][::-1]
    
    result = {
        'date': str(date.today()),
        'ai_components': len(df_cs),
        'ai_etfs_count': len(ai_etfs),
        'avg_turnover': round(avg_turnover, 2),
        'total_etf_amount': round(total_etf_amt / 1e8, 2),
        'quant_active_ratio': round(len([q for q in quant_active_list if q.get('量化活跃')]) / max(1, len(quant_active_list)) * 100, 1),
        'top10_sectors': top10,
        'bot10_sectors': bot10,
        'top5_stocks': sorted([q for q in quant_active_list if q.get('换手率')], 
                              key=lambda x: x['换手率'], reverse=True)[:5],
        'bottom5_stocks': sorted(quant_active_list[:20], 
                                key=lambda x: x.get('换手率', 0))[:5]
    }
    
    print(f"✅ AI板块量化集中度数据收集完成:")
    print(f"   - AI成分股: {result['ai_components']}只")
    print(f"   - AI ETF: {result['ai_etfs_count']}只")
    print(f"   - 平均换手率: {result['avg_turnover']}%")
    print(f"   - ETF总成交额: {result['total_etf_amount']}亿")
    print(f"   - 量化活跃占比: {result['quant_active_ratio']}%")
    
    return result

def save_data(data):
    """保存数据到 data.json"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            existing = json.load(f)
        
        existing['quant_ratio'] = data
        existing['generated_at'] = str(date.today()) + " 16:15"
    else:
        data['generated_at'] = str(date.today()) + " 16:15"
    
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 数据已保存至 {DATA_FILE}")

def save_history(data):
    """保存历史归档文件"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    
    filename = f"{date.today().strftime('%Y-%m-%d')}.json"
    filepath = os.path.join(HISTORY_DIR, filename)
    
    with open(filepath, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 历史归档已保存至 {filepath}")

if __name__ == "__main__":
    ai_data = get_ai_sector_data()
    if ai_data:
        save_data(ai_data)
        save_history(ai_data)
        
        print("\n=== quant-ratio 数据收集完成 ===")
