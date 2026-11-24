#!/bin/bash
# quant-ratio 投资看板 — 一键刷新脚本 v4
# 用法: ./run_quant_ratio.sh [theme]   (默认 theme=quant)
#
# v4 变更:
#   1. 渲染后跑 verify_quant.py 闸门 —— 校验不过就不提交、不推送
#   2. 推送加去重/冷却：同一信号当天只推一次，避免恒真信号天天刷屏
#   3. cron 首跑待验证：页面文案与 scripts 保持一致

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_PATH="${SCRIPT_DIR}"
PY=/Users/jren/miniforge3/bin/python3
THEME=${1:-quant}

echo "=== quant-ratio 投资看板刷新 (主题: ${THEME}) ==="

# 1. 收集数据 → quant_data.json + history/{theme}_{date}.json
echo "[1/5] 收集数据..."
${PY} "${DASHBOARD_PATH}/collect_quant_ratio.py" "${THEME}"

# 1b. 量化参与度：日频代理（A）+ 季度公募持仓下限（B，--if-stale 跳过）
echo "[1b/5] 量化活跃度代理 + 季度持仓..."
${PY} "${DASHBOARD_PATH}/collect_quant_activity.py" || echo "⚠️ 量化活跃度代理采集失败（非致命）"
${PY} "${DASHBOARD_PATH}/collect_quant_holdings.py" --if-stale || echo "⚠️ 量化持仓缓存刷新失败（非致命）"

# 2. 渲染 HTML（dashboard_investment.html + history/{theme}_{date}.html）
echo ""
echo "[2/5] 渲染HTML..."
cd "${DASHBOARD_PATH}" && ${PY} quant_ratio_render.py "${THEME}"

# 3. 自检闸门 —— 任一断言失败即中止（set -e），不提交脏数据、不推送
echo ""
echo "[3/5] 数据自检..."
${PY} "${DASHBOARD_PATH}/verify_quant.py"

# 4. 提交到 Git（如果已初始化）
if [ -d "${DASHBOARD_PATH}/.git" ]; then
    echo ""
    echo "[4/5] 提交Git..."
    cd "${DASHBOARD_PATH}"
    git add collect_quant_ratio.py collect_quant_activity.py collect_quant_holdings.py \
        quant_ratio_render.py verify_quant.py run_quant_ratio.sh \
        dashboard_investment.html quant_data.json risk_state.json \
        "history/${THEME}_*.html" "history/${THEME}_*.json" 2>/dev/null || true
    git commit -m "quant-ratio: update ${THEME} dashboard [$(date +%Y-%m-%d)]" 2>/dev/null \
        || echo "Git commit skipped (no changes)"
    git push origin main 2>/dev/null || echo "Git push failed (will retry next run)"
    echo "✅ Git committed + pushed."
else
    echo ""
    echo "[4/5] 跳过Git提交（未初始化）"
fi

# 5. 投资信号 → Telegram（无触发静默；同一信号当天只推一次）
echo ""
echo "[5/5] 检查投资信号..."
SIGNAL_FILE="${DASHBOARD_PATH}/quant_push_signal.txt"
# 推送逻辑收敛到 quant_push.py（与 T+1 终刷共用；去重键=数据日+信号hash）
if [ -f "${SIGNAL_FILE}" ] && ! grep -q "无触发" "${SIGNAL_FILE}"; then
    ${PY} "${DASHBOARD_PATH}/quant_push.py" || true
else
    echo "（无触发，静默）"
fi

echo ""
echo "=== quant-ratio ${THEME} 投资看板刷新完成 ==="
