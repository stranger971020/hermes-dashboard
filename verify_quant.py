#!/usr/bin/env python3
"""quant-ratio v5 数据自检闸门 — 任一失败 exit 1

v5 断言只围绕两件事：
  A. 模块①（量化集中度）下线后不得复活
  B. 国家队资金必须是「份额净申购」，不能退回「成交额」

用法:
  python3 verify_quant.py            # 校验 quant_data.json
  python3 verify_quant.py --soft     # 只报告不退出（调试用）

设计原则: 每条断言对应一次真实事故，不做形式化检查。
"""
import argparse
import glob
import json
import os
import sys

DATA = "/Users/jren/projects/hermes-dashboard/quant_data.json"
HISTORY_DIR = "/Users/jren/projects/hermes-dashboard/history"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--soft', action='store_true', help='只报告，不以非0退出')
    ap.add_argument('--data', default=DATA)
    args = ap.parse_args()

    d = json.load(open(args.data))
    errs, warns = [], []

    # 1) 交易日真实性：非交易日不应有当日数据
    try:
        import akshare as ak
        cal = {str(x) for x in ak.tool_trade_date_hist_sina()['trade_date']}
        if d.get('date') not in cal:
            errs.append(f"非交易日却有数据: {d.get('date')}")
    except Exception as e:  # noqa: BLE001
        warns.append(f"交易日历不可用，跳过第1项: {e}")

    # 2) 模块构成：必须恰好三个，量化集中度不得复活
    mods = d.get('modules') or []
    if len(mods) != 3:
        errs.append(f"模块数应为 3，实际 {mods}")
    if any('量化集中度' in m for m in mods):
        errs.append("量化集中度模块重新出现（已下线：换手率对未来收益/波动/回撤无预测力）")
    for k in ('ai_sectors', 'top10_sectors', 'bottom10_sectors', 'leaf_sectors', 'ranking_all'):
        if k in d:
            errs.append(f"残留旧模块字段: {k}")

    # 3) 国家队口径：必须是净申购，且与成交额区分开
    etf = d.get('etf') or {}
    net = etf.get('netbuy_series') or []
    if len(net) < 15:
        errs.append(f"netbuy_series 过短: {len(net)} 天（需 ≥15）")
    vals = [p.get('net_yi') for p in net if p.get('net_yi') is not None]
    if len(vals) < 10:
        errs.append(f"净申购有效点过少: {len(vals)}")
    elif all(abs(v) < 0.01 for v in vals):
        errs.append("净申购序列近乎全 0，疑似取数失败")
    if etf.get('netbuy_today_yi') is None:
        errs.append("今日净申购缺失")
    if not etf.get('netbuy_basis'):
        errs.append("缺少 netbuy_basis（口径说明）")
    if not etf.get('netbuy_coverage_pct'):
        errs.append("缺少 netbuy_coverage_pct（沪市覆盖比例）")
    amt = etf.get('broad_today_yi')
    if amt is None:
        errs.append("缺少当日宽基ETF成交额（背景数字）")
    elif etf.get('netbuy_today_yi') is not None and abs(etf['netbuy_today_yi']) > amt:
        errs.append(f"净申购({etf['netbuy_today_yi']}亿) 大于成交额({amt}亿)，口径可疑")

    # 4) 信号必须带分位/z 值；北向不得触发
    for s in (d.get('signals') or {}).get('signals', []):
        if '北向' in s.get('name', ''):
            if s.get('triggered'):
                errs.append("北向被判定为触发（该指标已停止披露）")
            continue
        if s.get('percentile') is None and s.get('zscore') is None:
            errs.append(f"信号缺分位/z值: {s.get('name')}")

    # 5) 主力资金序列可读
    mf = (d.get('flows') or {}).get('main_flow_series') or []
    if len(mf) < 15:
        errs.append(f"主力资金序列过短: {len(mf)}")

    # 6) 归档存在（页面靠它做索引）
    if not os.path.exists(os.path.join(HISTORY_DIR, f"quant_{d.get('date')}.html")):
        errs.append(f"归档文件缺失: history/quant_{d.get('date')}.html")
    if len(glob.glob(os.path.join(HISTORY_DIR, "quant_*.html"))) == 0:
        errs.append("history/ 下无 quant_*.html")

    print(f"quant-verify: {len(errs)} 错误 / {len(warns)} 警告  ({args.data})")
    for e in errs:
        print("  ❌", e)
    for w in warns:
        print("  ⚠️", w)
    if not errs:
        print("  ✅ 全部通过")
    return 0 if (args.soft or not errs) else 1


if __name__ == '__main__':
    sys.exit(main())
