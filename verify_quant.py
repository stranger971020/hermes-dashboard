#!/usr/bin/env python3
"""quant-ratio v4 数据自检闸门 — 任一失败 exit 1

用法:
  python3 verify_quant.py            # 校验 quant_data.json
  python3 verify_quant.py --soft     # 只报告不退出（调试用）

设计原则: 每条断言对应一次真实事故，不做形式化检查。
"""
import argparse
import itertools
import json
import sys

DATA = "/Users/jren/projects/hermes-dashboard/quant_data.json"
B_PREFIX = ('900', '200')


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

    # 2) 排名池计数齐全且单调递减
    u = d.get('universe') or {}
    missing = [k for k in ('scanned', 'exact_deduped', 'eligible', 'independent') if not u.get(k)]
    if missing:
        errs.append(f"universe 缺字段: {missing}")
    else:
        chain = [u['scanned'], u['exact_deduped'], u['eligible'], u['independent']]
        if not all(chain[i] >= chain[i + 1] for i in range(3)):
            errs.append(f"universe 计数不递减: {u}")

    # 3) 排名池不得存在包含关系（嵌套板块是本项目最大的口径事故）
    rank = d.get('ranking_all') or []
    if not rank:
        errs.append("ranking_all 缺失 —— 排名无法被第三方复核")
    else:
        sets = {}
        for r in rank:
            if r.get('codes'):
                sets[r['板块']] = set(str(c) for c in r['codes'])
        bad = 0
        for a, b in itertools.combinations(sets, 2):
            if sets[a] < sets[b] or sets[b] < sets[a]:
                errs.append(f"排名池仍有包含关系: {a} ⊂/⊃ {b}")
                bad += 1
                if bad >= 5:
                    errs.append("（仅列前5条）")
                    break
        if u.get('independent') and len(rank) != u['independent']:
            errs.append(f"ranking_all {len(rank)} != independent {u['independent']}")

    # 4) 每个信号必须带分位（除北向），否则等于没有参照系
    for s in (d.get('signals') or {}).get('signals', []):
        if '北向' in s.get('name', ''):
            if s.get('triggered'):
                errs.append("北向被判定为触发（该指标已停止披露）")
            continue
        if s.get('percentile') is None and s.get('zscore') is None:
            errs.append(f"信号缺分位/z值: {s.get('name')}")

    # 5) 榜单不得出现 B股 / 停牌 / 零换手
    for blk in (d.get('ai_sectors', []) + d.get('top10_sectors', [])
                + d.get('bottom10_sectors', []) + d.get('leaf_sectors', [])):
        for r in blk.get('top5_stocks', []) + blk.get('bottom5_stocks', []):
            code = str(r.get('代码', ''))
            if code.startswith(B_PREFIX):
                errs.append(f"B股进入榜单: {blk.get('板块')} {r.get('名称')}")
            if (r.get('换手率%') or 0) == 0 and not (r.get('成交额亿') or 0) > 0:
                errs.append(f"零换手(停牌)进入榜单: {blk.get('板块')} {r.get('名称')}")

    # 6) 集中度自洽：活跃数/可交易数 == 展示值
    for blk in (d.get('ai_sectors', []) + d.get('top10_sectors', [])
                + d.get('bottom10_sectors', []) + d.get('leaf_sectors', [])):
        n = blk.get('可交易股数')
        a = blk.get('量化活跃股数')
        r = blk.get('量化集中度%_今日')
        if n is None:
            errs.append(f"缺『可交易股数』: {blk.get('板块')}")
            continue
        if r is not None and n:
            if abs(round(a / n * 100, 1) - r) > 0.2:
                errs.append(f"集中度不自洽: {blk.get('板块')} {a}/{n} != {r}")

    # 7) 归档可访问性（文件存在，页面才能链）
    import glob
    import os
    latest = f"/Users/jren/projects/hermes-dashboard/history/quant_{d.get('date')}.html"
    if not os.path.exists(latest):
        errs.append(f"归档文件缺失: {latest}")
    if len(glob.glob("/Users/jren/projects/hermes-dashboard/history/quant_*.html")) == 0:
        errs.append("history/ 下无 quant_*.html 归档")

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
