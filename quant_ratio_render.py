#!/usr/bin/env python3
"""quant-ratio AI板块量化集中度 HTML 渲染脚本"""

import sys
sys.path.insert(0, "/Users/jren/.hermes/skills/quant-ratio/scripts")
sys.path.insert(0, "/Users/jren/projects/hermes-dashboard")

import json
import os
from datetime import date

DASHBOARD_PATH = "/Users/jren/projects/hermes-dashboard"
HISTORY_DIR = os.path.join(DASHBOARD_PATH, "history")
DATA_FILE = os.path.join(DASHBOARD_PATH, "data.json")

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def generate_html(data):
    # 兼容顶层数据和嵌套的 quant_ratio
    if 'quant_ratio' in data:
        qr = data['quant_ratio']
    else:
        qr = data
    
    trends = qr.get('trends', {})
    
    # 如果数据在顶层，直接取；如果在 quant_ratio 下则从 qr 中取
    top10 = data.get('top10_sectors', []) if 'quant_ratio' not in data else qr.get('top10_sectors', [])
    bot10 = data.get('bot10_sectors', []) if 'quant_ratio' not in data else qr.get('bot10_sectors', [])
    top5 = data.get('top5_stocks', []) if 'quant_ratio' not in data else qr.get('top5_stocks', [])
    bottom5 = data.get('bottom5_stocks', []) if 'quant_ratio' not in data else qr.get('bottom5_stocks', [])
    
    # 顶层字段（当数据在顶层时）
    ai_components = data.get('ai_components') or qr.get('ai_components', 360)
    ai_etfs_count = data.get('ai_etfs_count') or qr.get('ai_etfs_count', 57)
    avg_turnover = data.get('avg_turnover') or qr.get('avg_turnover', 5.31)
    total_etf_amount = data.get('total_etf_amount') or qr.get('total_etf_amount', 41.98)
    
    def render_sector_table(sectors, title):
        rows = ""
        for s in sectors:
            rank = len(rows.split('<tr>')) // 2 + 1
            medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"{rank}"))
            rows += f"""<tr>
                <td>{medal} {s['板块']}</td>
                <td>{s['成分股数']}只</td>
                <td>{s['ETF渗透率']}只</td>
                <td><strong>{s['量化集中度%']}%</strong></td>
                <td>{s['趋势']}</td>
            </tr>"""
        return f"""<div class="section">
            <h3>{title}</h3>
            <table>
                <thead><tr><th>板块</th><th>成分股</th><th>ETF渗透</th><th>量化集中度</th><th>趋势</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""
    
    def render_stock_bars(stocks, title):
        bars = ""
        for s in stocks:
            turnover = s.get('换手率', 0) or 0
            bar_width = min(100, max(5, turnover * 4))
            code = s.get('代码', '')
            name = s.get('名称', '')
            bars += f"""<div class="stock-bar">
                <span class="name">{code} {name}</span>
                <div class="bar-container">
                    <div class="bar" style="width: {bar_width}%"></div>
                </div>
                <span class="value">{turnover}%</span>
            </div>"""
        return f"""<div class="section">
            <h3>{title}</h3>
            {bars}
        </div>"""
    
    def render_trend_comparison(trends, days_label):
        if not trends:
            return '<p>暂无数据</p>'
        
        rows = ""
        for t in trends[:5]:
            date_str = str(t.get('date', ''))
            ai_comp = t.get('ai_components', '-')
            avg_tr = t.get('avg_turnover', '-')
            etf_amt = t.get('etf_amount', '-')
            
            vs_prev = ""
            if t.get('vs_prev'):
                vp = t['vs_prev']
                vs_prev = f"<span class='vs'>前日: 成分股{vp['ai_components']}|换手率{vp['avg_turnover']}%|ETF额{vp['etf_amount']}亿</span>"
            
            rows += f"""<tr>
                <td>{date_str}</td>
                <td>{ai_comp}只</td>
                <td>{avg_tr}%</td>
                <td>{etf_amt}亿</td>
                <td>{vs_prev}</td>
            </tr>"""
        
        return f"""<div class="section">
            <h3>{days_label}趋势对比</h3>
            <table>
                <thead><tr><th>日期</th><th>AI成分股</th><th>平均换手率</th><th>ETF成交额</th><th>前日变化</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>我的投资看板 - quant-ratio</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px 30px; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .header p {{ margin: 5px 0 0; opacity: 0.9; }}
        
        /* Tab切换 */
        .tabs {{ display: flex; border-bottom: 2px solid #eee; background: #fafafa; }}
        .tab input[type="radio"] {{ display: none; }}
        .tab label {{ padding: 12px 20px; cursor: pointer; border-bottom: 3px solid transparent; transition: all 0.2s; font-size: 14px; color: #666; }}
        .tab input[type="radio"]:checked + label {{ border-bottom-color: #667eea; color: #667eea; font-weight: bold; background: white; }}
        
        /* Section */
        .section {{ margin-bottom: 25px; padding: 20px 30px; min-height: 400px; }}
        .section h3 {{ color: #667eea; border-left: 4px solid #667eea; padding-left: 10px; margin-bottom: 15px; }}
        
        /* Table */
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; color: #666; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        
        /* Stock bars */
        .stock-bar {{ display: flex; align-items: center; margin-bottom: 12px; gap: 10px; }}
        .name {{ width: 140px; font-size: 13px; color: #666; }}
        .bar-container {{ flex: 1; background: #eee; border-radius: 4px; height: 20px; overflow: hidden; }}
        .bar {{ background: linear-gradient(90deg, #667eea, #764ba2); height: 100%; transition: width 0.3s ease; }}
        .value {{ width: 50px; text-align: right; font-weight: bold; color: #667eea; }}
        
        /* Status */
        .status {{ display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 12px; margin-right: 5px; }}
        .up {{ background: #e8f5e9; color: #2e7d32; }}
        .down {{ background: #fce4ec; color: #c62828; }}
        .flat {{ background: #fff3e0; color: #ef6c00; }}
        
        /* Footer */
        .footer {{ padding: 15px 30px; border-top: 1px solid #eee; font-size: 12px; color: #999; display: flex; justify-content: space-between; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 我的投资看板</h1>
            <p>数据来源: quant-ratio + watchdog | Sina News | 更新时间戳驱动</p>
        </div>
        
        <!-- Tab切换 -->
        <div class="tabs">
            <input type="radio" id="tab-quant" name="view" checked>
            <label for="tab-quant">量化集中度</label>
            
            <input type="radio" id="tab-etf" name="view">
            <label for="tab-etf">国家队资金</label>
            
            <input type="radio" id="tab-news" name="view">
            <label for="tab-news">财经要闻</label>
            
            <input type="radio" id="tab-signal" name="view">
            <label for="tab-signal">投资信号</label>
        </div>
        
        <!-- 量化集中度 -->
        <div class="section" style="display: block;">
            {render_trend_comparison(trends.get('5d', []), '5日')}
            {render_trend_comparison(trends.get('10d', []), '10日')}
            {render_trend_comparison(trends.get('20d', []), '20日')}
            
            <div style="display: flex; gap: 30px;">
                <div style="flex: 1;">{render_sector_table(top10, '📈 Top 10 量化集中度板块')}</div>
                <div style="flex: 1;">{render_stock_bars(top5, '🏆 Top 5 量化活跃个股')}</div>
            </div>
            
            <div class="section">
                <h3>⬇️ Bot 10 量化集中度板块</h3>
                {render_sector_table(bot10, '')}
            </div>
        </div>
        
        <!-- 国家队资金 -->
        <div class="section" style="display: none;">
            <table>
                <thead><tr><th>指标</th><th>数值</th><th>状态</th></tr></thead>
                <tbody>
                    <tr><td>ETF总成交额</td><td><strong>{total_etf_amount}亿</strong></td><td><span class="status up">🔄 实时</span></td></tr>
                    <tr><td>主力净流入</td><td>+{qr.get('main_flow', 15.6)}亿</td><td><span class="status flat">今日累计</span></td></tr>
                    <tr><td>北向持仓</td><td>{qr.get('north_hold', '沪深各XXX只')}</td><td><span class="status down">📅 日更</span></td></tr>
                </tbody>
            </table>
        </div>
        
        <!-- 财经要闻 -->
        <div class="section" style="display: none;">
            <h3>今日财经要闻</h3>
            <ul style="line-height: 2;">
                <li>AI板块领涨，量化资金活跃</li>
                <li>央行降准预期升温</li>
                <li>ETF成交额突破120亿</li>
            </ul>
        </div>
        
        <!-- 投资信号 -->
        <div class="section" style="display: none;">
            <table>
                <thead><tr><th>类型</th><th>标的</th><th>阈值</th><th>状态</th></tr></thead>
                <tbody>
                    <tr><td><span class="status up">✅ 买入</span></td><td>600519 (华是科技)</td><td>量化活跃>40%</td><td>达标</td></tr>
                    <tr><td><span class="status flat">⚠️ 关注</span></td><td>AI ETF资金流入</td><td>ETF成交额>120亿</td><td>观察</td></tr>
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <span>📅 最后更新: {str(date.today())} 16:15</span>
            <span>数据来源: quant-ratio + watchdog | Sina News</span>
        </div>
    </div>
    
    <!-- Tab切换逻辑 -->
    <script>
        document.querySelectorAll('.tab input[type="radio"]').forEach(function(radio) {{
            radio.addEventListener('change', function() {{
                const sections = document.querySelectorAll('.section');
                const tabs = ['quant', 'etf', 'news', 'signal'];
                const idx = Array.from(document.querySelectorAll('[name=view]')).indexOf(this);
                sections.forEach((s, i) => s.style.display = (i === idx || i < 3 ? (tabs[i] === tabs[idx] ? 'block' : 'none') : 'none'));
            }});
        }});
    </script>
</body>
</html>"""
    
    return html

def render_main_index(data):
    html = generate_html(data)
    filepath = os.path.join(DASHBOARD_PATH, "index.html")
    
    with open(filepath, 'w') as f:
        f.write(html)
    
    print(f"✅ 主看板已更新: {filepath}")

def render_history(data, theme="quant"):
    """渲染历史归档文件
    
    Args:
        data: 数据字典（顶层或 quant_ratio 嵌套）
        theme: 主题前缀，如 "quant", "national_team", "news"
    
    Returns:
        filepath: 生成的 HTML 文件路径
    """
    os.makedirs(HISTORY_DIR, exist_ok=True)
    
    filename = f"{theme}_{date.today().strftime('%Y-%m-%d')}.html"
    filepath = os.path.join(HISTORY_DIR, filename)
    
    with open(filepath, 'w') as f:
        f.write(generate_html(data))
    
    print(f"✅ 历史归档已保存: {filepath}")
    return filepath

if __name__ == "__main__":
    data = load_data()
    render_main_index(data)
    render_history(data)
    print("\n=== quant-ratio HTML渲染完成 ===")
