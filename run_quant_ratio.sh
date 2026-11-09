#!/bin/bash
# quant-ratio AI板块量化集中度 — 一键刷新脚本 v2
# 用法: ./run_quant_ratio.sh [theme]   (默认 theme=quant)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_PATH="${SCRIPT_DIR}"
PY=/Users/jren/miniforge3/bin/python3

# 主题参数（从命令行传入，默认 "quant"）
THEME=${1:-quant}

echo "=== quant-ratio 投资看板刷新 (主题: ${THEME}) ==="

# 1. 收集数据 → quant_data.json + history/{theme}_{date}.json
echo "[1/3] 收集量化集中度数据..."
${PY} "${DASHBOARD_PATH}/collect_quant_ratio.py" "${THEME}"

# 2. 渲染 HTML（dashboard_investment.html + history/{theme}_{date}.html）
echo ""
echo "[2/3] 渲染HTML页面..."
cd "${DASHBOARD_PATH}" && ${PY} quant_ratio_render.py "${THEME}"

# 3. 提交到 Git (如果已初始化)
if [ -d "${DASHBOARD_PATH}/.git" ]; then
    echo ""
    echo "[3/3] 提交Git..."
    cd "${DASHBOARD_PATH}"
    git add collect_quant_ratio.py quant_ratio_render.py run_quant_ratio.sh dashboard_investment.html quant_data.json "history/${THEME}_*.html" "history/${THEME}_*.json" 2>/dev/null || true
    git commit -m "quant-ratio: update ${THEME} dashboard [$(date +%Y-%m-%d)]" 2>/dev/null || echo "Git commit skipped (no changes)"
    git push origin main 2>/dev/null || echo "Git push failed (will retry next run)"
    echo "✅ Git committed + pushed."
else
    echo ""
    echo "[3/3] 跳过Git提交（未初始化）"
fi

echo ""
echo "=== quant-ratio ${THEME} 投资看板刷新完成 ==="
