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
import re
import sys

DATA = "/Users/jren/projects/hermes-dashboard/quant_data.json"
HISTORY_DIR = "/Users/jren/projects/hermes-dashboard/history"
V6_DIR = "/Users/jren/projects/trend-trading-system/reports/v6_daily"


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
    incomplete_today = etf.get('netbuy_today_complete') is False
    if incomplete_today:
        miss = '、'.join(etf.get('netbuy_today_missing') or []) or '单侧市场'
        warns.append(f"今日净申购不完整（缺 {miss}）：已标 incomplete，不计分位、不触发信号，"
                     f"成交量级断言跳过（单口径 vs 合计口径不可比）")
    elif amt is None:
        errs.append("缺少当日宽基ETF成交额（背景数字）")
    elif etf.get('netbuy_today_yi') is not None and abs(etf['netbuy_today_yi']) > amt:
        errs.append(f"净申购({etf['netbuy_today_yi']}亿) 大于成交额({amt}亿)，口径可疑")

    # 4) 信号必须带分位/z 值；北向不得触发；标记 incomplete 的信号不得触发
    for s in (d.get('signals') or {}).get('signals', []):
        if '北向' in s.get('name', ''):
            if s.get('triggered'):
                errs.append("北向被判定为触发（该指标已停止披露）")
            continue
        if s.get('data_incomplete'):
            if s.get('triggered'):
                errs.append(f"数据不完整却判定触发: {s.get('name')}")
            continue
        if s.get('percentile') is None and s.get('zscore') is None:
            errs.append(f"信号缺分位/z值: {s.get('name')}")

    # 5) 主力资金序列可读
    #    源断连属外部时效问题 → warn，不阻塞当日推送；
    #    但「序列为空且没有 main_flow_error」= 采集静默丢数（2026-11-23 教训）→ error。
    flows = d.get('flows') or {}
    mf = flows.get('main_flow_series') or []
    if len(mf) < 15:
        err = flows.get('main_flow_error')
        if not err:
            errs.append(f"主力资金序列过短: {len(mf)}，且无 main_flow_error 记录（静默丢数）")
        else:
            warns.append(f"主力资金序列过短: {len(mf)}（{str(err)[:80]}；页面显示数据缺失，不阻塞推送）")

    # 6) 归档存在（页面靠它做索引）
    if not os.path.exists(os.path.join(HISTORY_DIR, f"quant_{d.get('date')}.html")):
        errs.append(f"归档文件缺失: history/quant_{d.get('date')}.html")
    if len(glob.glob(os.path.join(HISTORY_DIR, "quant_*.html"))) == 0:
        errs.append("history/ 下无 quant_*.html")

    # 7) V6 摘要（跨系统契约, 2026-11-21）—— 新鲜度问题是 warn：V6 周末/节假日不产报,
    #    不应阻断本看板发布；结构问题（schema/关键字段）才是 err。
    v6_files = sorted(glob.glob(os.path.join(V6_DIR, "v6_daily_*.json")))
    if not v6_files:
        warns.append("V6 摘要 JSON 缺失（看板 ④ 段将显示『尚未生成』）")
    else:
        try:
            with open(v6_files[-1], encoding="utf-8") as f:
                v6 = json.load(f)
            if v6.get("schema") != 1:
                errs.append(f"V6 摘要 schema={v6.get('schema')}，期望 1（{os.path.basename(v6_files[-1])}）")
            for k in ("temperature", "decision_card", "position"):
                if k not in v6:
                    errs.append(f"V6 摘要缺关键字段: {k}")
            rg = v6.get("risk_guard")
            if not isinstance(rg, dict):
                warns.append("V6 摘要缺 risk_guard（看板 ⑦ 将回退 risk_state.json）")
            elif str(rg.get("asof")) != str(v6.get("trading_date")):
                errs.append(f"risk_guard.asof={rg.get('asof')} 与 trading_date={v6.get('trading_date')} 不一致")
            if not v6.get("links", {}).get("html"):
                warns.append("V6 摘要缺 links.html（看板外链将回退拼 URL）")
            try:
                import datetime as _dt
                _td = _dt.datetime.strptime(str(v6.get("trading_date")), "%Y%m%d").date()
                _age = (_dt.date.today() - _td).days
                if _age > 5:
                    warns.append(f"V6 摘要已 {_age} 天未更新（trading_date={v6.get('trading_date')}）")
            except Exception:  # noqa: BLE001
                errs.append("V6 摘要 trading_date 不可解析")
        except Exception as e:  # noqa: BLE001
            errs.append(f"V6 摘要解析失败: {e}")

    # 8) 政策信号摘要（跨系统契约, 2026-11-21）—— 结构问题=err，陈旧/降级=warn
    pol_file = os.path.join(os.path.dirname(DATA), "policy", "latest.json")
    if not os.path.exists(pol_file):
        warns.append("政策摘要缺失（看板 ⑤ 段将显示『尚未生成』）")
    else:
        try:
            with open(pol_file, encoding="utf-8") as f:
                pol = json.load(f)
            if pol.get("schema") != 1:
                errs.append(f"政策摘要 schema={pol.get('schema')}，期望 1")
            for k in ("score", "band", "cap", "market", "recent", "breakdown"):
                if k not in pol:
                    errs.append(f"政策摘要缺关键字段: {k}")
            # 口径可复现闸门：score == 力度 × 位置系数 − 杠杆罚分（veto 时归零）
            try:
                calc = 0.0
                if not pol.get("veto"):
                    calc = round(float(pol["effect_score"]) * float(pol["position_coef"])
                                 - float(pol["leverage_penalty"]), 1)
                if abs(calc - float(pol["score"])) > 0.15:
                    errs.append(f"政策分不可复现: 记录 {pol['score']} vs 公式 {calc}")
            except Exception as e:  # noqa: BLE001
                warns.append(f"政策分复算跳过（字段不全）: {e}")
            if pol.get("degraded"):
                warns.append(f"政策摘要自报数据降级: {pol.get('coverage')}")
            iso = pol.get("trade_date_iso") or ""
            try:
                import datetime as _dt
                _pd = _dt.datetime.strptime(str(iso), "%Y-%m-%d").date()
                _pdays = (_dt.date.today() - _pd).days
                if _pdays > 7:
                    warns.append(f"政策摘要已 {_pdays} 天未更新（{iso}）")
            except Exception:  # noqa: BLE001
                warns.append(f"政策摘要 trade_date_iso 不可解析: {iso!r}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"政策摘要解析失败: {e}")

    # 9) 情绪温度计段（⑥, 2026-11-21）—— 数字必须能回算，且不得出现「卖出」指令
    HTML = os.path.join(os.path.dirname(DATA), "dashboard_investment.html")
    if os.path.exists(HTML):
        try:
            html = open(HTML, encoding="utf-8").read()
            if "⑥ 情绪温度计" not in html:
                errs.append("看板缺 ⑥ 情绪温度计段（render_module_emotion 未挂载）")
            else:
                seg = html.split("⑥ 情绪温度计", 1)[1][:6000]
                if "非卖出信号" not in seg:
                    errs.append("⑥ 段缺「非卖出信号」定位说明（易被误读为卖出指令）")
                # 涨跌家数比必须能在 V6 摘要里对得上
                v6f = sorted(glob.glob(os.path.join(V6_DIR, "v6_daily_*.json")))
                if not v6f:
                    warns.append("⑥ 段：无 V6 摘要可比对")
                else:
                    s6 = json.load(open(v6f[-1], encoding="utf-8"))
                    e6 = s6.get("emotion") or {}
                    ur = e6.get("updown_ratio")
                    if isinstance(ur, (int, float)):
                        shown = f"{ur * 100:.1f}%"
                        if shown not in seg:
                            errs.append(f"⑥ 段涨跌家数比与摘要不符: 摘要 {shown} 未出现在页面")
                    else:
                        warns.append("⑥ 段：摘要无 updown_ratio")
                    # 禁止把退潮写成清仓建议
                    for bad in ("清仓", "立即卖出", "全部卖出"):
                        if bad in seg:
                            errs.append(f"⑥ 段出现卖出指令「{bad}」—— 与实测结论矛盾（退潮后未来5日反而偏正）")
        except Exception as e:  # noqa: BLE001
            errs.append(f"⑥ 情绪段校验失败: {e}")

    # 10) 浮盈保护段（⑦, 2026-11-21）—— 分数可回算、动作与状态一致、不得写成卖出指令
    risk_file = os.path.join(os.path.dirname(DATA), "risk_state.json")
    rk = None
    _src = None
    _v6_files = sorted(glob.glob(os.path.join(V6_DIR, "v6_daily_*.json")))
    if _v6_files:
        try:
            with open(_v6_files[-1], encoding="utf-8") as f:
                _v6_rk = json.load(f)
            if isinstance(_v6_rk.get("risk_guard"), dict):
                rk = _v6_rk["risk_guard"]
                _src = "v6_summary"
        except Exception:  # noqa: BLE001
            pass
    if rk is None and os.path.exists(risk_file):
        try:
            with open(risk_file, encoding="utf-8") as f:
                rk = json.load(f)
            _src = "file_fallback"
        except Exception as e:  # noqa: BLE001
            errs.append(f"风险状态解析失败: {e}")
    if rk is None:
        warns.append("风险状态缺失（V6 摘要无 risk_guard，risk_state.json 也不存在）")
    else:
        try:
            if rk.get("schema") != 1:
                errs.append(f"风险状态 schema={rk.get('schema')}，期望 1")
            for k in ("asof", "state", "score", "components", "action", "metrics"):
                if k not in rk:
                    errs.append(f"风险状态缺关键字段: {k}")
            comp = rk.get("components") or {}
            score = int(sum(bool(v) for v in comp.values()))
            if score != int(rk.get("score", -1)):
                errs.append(f"风险分数不可复现: 记录 {rk.get('score')} vs 组件和 {score}")
            state = ("normal" if score <= 1 else "warning" if score <= 3
                     else "fragile" if score <= 5 else "crash")
            if state != rk.get("state"):
                errs.append(f"风险状态与分数不符: score {score} → {state}，记录 {rk.get('state')}")
            base = {"normal": 0.0, "warning": 0.0, "fragile": 0.25, "crash": 0.40}[state]
            m = rk.get("metrics") or {}
            trim = base
            if (m.get("g60") or 0) < -0.05 and int(m.get("shock10") or 0) >= 2:
                trim = max(trim, 0.50)
            trim = min(trim, 0.50)
            act = rk.get("action") or {}
            _fresh = rk.get("freshness") or {}
            _ok = bool(rk.get("ok", True))
            _stale = int(rk.get("stale_days", _fresh.get("stale_days", 0)) or 0)
            if _ok and _stale == 0 and abs(float(act.get("initial_trim", -1)) - trim) > 1e-9:
                errs.append(f"初减建议不可复现: 记录 {act.get('initial_trim')} vs 规则 {trim}")
            try:
                with open(os.path.expanduser(
                        "~/projects/trading-journal/config/exit_rules.json"),
                        encoding="utf-8") as _ef:
                    _er = json.load(_ef)
                _trail = float(_er.get("trail_dd_pct", 0.08))
            except Exception:  # noqa: BLE001
                _trail = 0.08
            if abs(float(act.get("trail_stop", -1)) - _trail) > 1e-9:
                errs.append(f"移动止盈回撤应为 {_trail:.0%}，记录 {act.get('trail_stop')}")
            if not (rk.get("validation") or {}).get("fragile_crash_15"):
                warns.append("风险状态缺回测锚点（先跑 backtest/rb_export.py）")
            # 页面段
            if os.path.exists(HTML):
                html10 = open(HTML, encoding="utf-8").read()
                if "<h2>⑦ 浮盈保护" not in html10:
                    errs.append("看板缺 ⑦ 浮盈保护段（render_module_risk 未挂载）")
                else:
                    seg10 = html10.split("<h2>⑦ 浮盈保护", 1)[1][:12000]
                    for mk in ("建议初减", "移动止盈计算器", "回测锚点",
                               "与 V6 分工", "防守/已有浮盈"):
                        if mk not in seg10:
                            errs.append(f"⑦ 段缺标记: {mk}")
                    # 2026-11-27: 口径统一为「浮盈曾达+10% → 峰值回撤8%清仓剩余」。
                    # 「清仓」不再是一键卖光禁词——它只作用于已激活的剩余仓位；
                    # 仍禁止无差别的立即/全部卖出表述。
                    for need in ("+10%", "清仓"):
                        if need not in seg10:
                            errs.append(f"⑦ 段缺出场口径标记: {need}")
                    if _ok and _stale == 0 and f"{trim:.0%}" not in seg10:
                        errs.append(f"⑦ 段建议初减与状态不符: 期望 {trim:.0%}")
                    if (not _ok or _stale) and not any(x in seg10 for x in ("暂停", "风险状态未计算")):
                        errs.append("⑦ 段风险状态不可用/滞后，但页面没有暂停提示")
                    for bad in ("立即卖出", "全部卖出"):
                        if bad in seg10:
                            errs.append(f"⑦ 段出现无差别卖出指令「{bad}」—— "
                                        f"初减是部分动作；只有移动止盈触发才清仓剩余")
            # 陈旧告警
            try:
                import datetime as _dt
                _ad = _dt.datetime.strptime(str(rk.get("asof")), "%Y%m%d").date()
                _days = (_dt.date.today() - _ad).days
                if _days > 7:
                    warns.append(f"风险状态已 {_days} 天未更新（asof {rk.get('asof')}）")
            except Exception:  # noqa: BLE001
                warns.append(f"风险状态 asof 不可解析: {rk.get('asof')!r}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"风险状态校验失败: {e}")

    # 11) 量化活跃度代理（⑧, 2026-11-22）—— 必须自证是代理，不得冒充占比
    act_file = os.path.join(os.path.dirname(DATA), "cache", "quant_activity.json")
    if not os.path.exists(act_file):
        warns.append("量化活跃度代理 cache/quant_activity.json 缺失（看板 ⑧ 段将显示未生成）")
    else:
        try:
            with open(act_file, encoding="utf-8") as f:
                a = json.load(f)
            if a.get("schema") != 1:
                errs.append(f"量化代理 schema={a.get('schema')}，期望 1")
            for k in ("asof", "market", "coverage", "proxy"):
                if k not in a:
                    errs.append(f"量化代理缺关键字段: {k}")
            m = a.get("market") or {}
            for k in ("score", "micro_score", "large_score", "spread_score"):
                v = m.get(k)
                if not isinstance(v, (int, float)) or not (0 <= v <= 100):
                    errs.append(f"量化代理 {k} 不是 0-100: {v!r}")
            caveat = (a.get("proxy") or {}).get("caveat", "")
            if "不是" not in caveat and "无法识别" not in caveat:
                errs.append("量化代理缺「不是成交占比/无法识别」的自我限定")
            if not a.get("top_stocks"):
                warns.append("量化代理 top_stocks 为空")
            if os.path.exists(HTML):
                html11 = open(HTML, encoding="utf-8").read()
                if "<h2>⑧ 量化参与度" not in html11:
                    errs.append("看板缺 ⑧ 量化参与度段（render_module_quant_activity 未挂载）")
                else:
                    seg11 = html11.split("<h2>⑧ 量化参与度", 1)[1][:14000]
                    for mk in ("活跃度代理", "不是成交占比", "微盘活跃度",
                               "个股活跃度 Top10", "2024-01 微盘崩"):
                        if mk not in seg11:
                            errs.append(f"⑧ 段缺标记: {mk}")
                    if "量化持仓占比" in seg11 and "不是" not in seg11:
                        errs.append("⑧ 段出现未限定的「量化持仓占比」")
        except Exception as e:  # noqa: BLE001
            errs.append(f"量化代理校验失败: {e}")

    # 12) 量化持仓（⑨, 季度·公募下限）—— 必须标「仅公募/不含私募/下限」
    hold_file = os.path.join(os.path.dirname(DATA), "cache", "quant_holdings.json")
    if not os.path.exists(hold_file):
        warns.append("量化持仓 cache/quant_holdings.json 缺失（看板 ⑨ 段将显示未生成）")
    else:
        try:
            with open(hold_file, encoding="utf-8") as f:
                hd = json.load(f)
            if hd.get("schema") != 1:
                errs.append(f"量化持仓 schema={hd.get('schema')}，期望 1")
            for k in ("quarter", "coverage", "market", "top_industries"):
                if k not in hd:
                    errs.append(f"量化持仓缺关键字段: {k}")
            cov = hd.get("coverage") or {}
            ok = int(cov.get("n_funds_ok") or 0)
            base = int(cov.get("n_funds_base") or 1)
            ratio = ok / base if base else 0
            if ratio >= 0.8 and not isinstance((hd.get("market") or {}).get("share_pct"), (int, float)):
                errs.append("量化持仓覆盖率足够但 share_pct 非数值")
            if os.path.exists(HTML):
                html12 = open(HTML, encoding="utf-8").read()
                if "<h2>⑨ 量化持仓" not in html12:
                    errs.append("看板缺 ⑨ 量化持仓段（render_module_quant_holdings 未挂载）")
                else:
                    seg12 = html12.split("<h2>⑨ 量化持仓", 1)[1][:12000]
                    for mk in ("仅公募", "不含私募", "下限", "个股持仓占流通比 Top10"):
                        if mk not in seg12:
                            errs.append(f"⑨ 段缺标记: {mk}")
                    if ratio < 0.8 and "覆盖不足" not in seg12:
                        errs.append(f"⑨ 段覆盖率 {ok}/{base} 不足，但页面没有覆盖不足提示")
                    if "全部量化持仓" in seg12 and "不是" not in seg12:
                        errs.append("⑨ 段把公募前十大说成全部量化持仓")
        except Exception as e:  # noqa: BLE001
            errs.append(f"量化持仓校验失败: {e}")

    print(f"quant-verify: {len(errs)} 错误 / {len(warns)} 警告  ({args.data})")
    # 13) 九段标题行必须带「刷新 MM-DD HH:MM」（2026-11-23）
    #     各段产出时点不同（V6 凌晨 02:45 / 要闻早间 / 政策 17:30 EOD / ①③ 本次 collect），
    #     只写日期会把「凌晨的数据」误读成刚刷新。缺 tag = 渲染器回退或漏挂。
    if os.path.exists(HTML):
        try:
            _h = open(HTML, encoding="utf-8").read()
            _secs = [("sec-national", "①"), ("sec-news", "②"), ("sec-signals", "③"),
                     ("sec-v6", "④"), ("sec-policy", "⑤"), ("sec-emotion", "⑥"),
                     ("sec-risk", "⑦"), ("sec-quant", "⑧"), ("sec-holdings", "⑨")]
            _miss = []
            for _sid, _num in _secs:
                _i = _h.find(f'id="{_sid}"')
                _j = _h.find("</h2>", _i) if _i >= 0 else -1
                if _i < 0 or _j < 0 or not re.search(r"刷新 \d{2}-\d{2} \d{2}:\d{2}",
                                                     _h[_i:_j]):
                    _miss.append(_num)
            if _miss:
                errs.append(f"模块标题缺「刷新 MM-DD HH:MM」: {'/'.join(_miss)}")
        except Exception as e:  # noqa: BLE001
            warns.append(f"刷新时间检查失败: {e}")

    # 14) ① 段必须拆分「沪市 / 深市 / 沪深合计」并为沪市标注 T+1（2026-11-24）
    #     背景: 上交所 T+1、深交所当日；不拆开会把「沪市未发布」误读成 0 或单市场信号。
    if os.path.exists(HTML):
        try:
            _h1 = open(HTML, encoding="utf-8").read()
            _i = _h1.find('id="sec-national"')
            _seg = _h1[_i:_h1.find('</section>', _i)] if _i >= 0 else ''
            _miss = [c for c in ('沪市净申购(亿)', '深市净申购(亿)', '沪深合计(亿)')
                     if c not in _seg]
            if _miss:
                errs.append(f"① 段缺沪深拆分列: {'/'.join(_miss)}")
            if 'T+1' not in _seg:
                errs.append("① 段缺「沪市 T+1」口径说明（未发布会与 0 混淆）")
        except Exception as e:  # noqa: BLE001
            warns.append(f"① 沪深拆分列检查失败: {e}")

    for e in errs:
        print("  ❌", e)
    for w in warns:
        print("  ⚠️", w)
    if not errs:
        print("  ✅ 全部通过")
    return 0 if (args.soft or not errs) else 1


if __name__ == '__main__':
    sys.exit(main())
