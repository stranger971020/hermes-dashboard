#!/bin/bash
# quant-ratio AI板块量化集中度 — 一键刷新脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_PATH="${SCRIPT_DIR}"

# 主题参数（从命令行传入，默认 "quant"）
THEME=${1:-quant}

echo "=== quant-ratio 投资看板刷新 (主题: ${THEME}) ===\n"

# 1. 收集数据
echo "[1/3] 收集量化集中度数据..."
python3 "${DASHBOARD_PATH}/collect_quant_ratio.py"

# 2. 渲染 HTML（带主题前缀的历史归档）
echo "\n[2/3] 渲染HTML页面..."
python3 -c "
import sys, os
sys.path.insert(0, '${DASHBOARD_PATH}')
from quant_ratio_render import load_data, render_main_index, render_history

data = load_data()
render_main_index(data)
render_history(data, theme='${THEME}')
"

# 3. 提交到 Git (如果已初始化)
if [ -d "${DASHBOARD_PATH}/.git" ]; then
    echo "\n[3/3] 提交Git..."
    cd "${DASHBOARD_PATH}"
    git add index.html history/*_${THEME}_*.html data.json 2>/dev/null || true
    git commit -m "quant-ratio: update ${THEME} dashboard [$(date +%Y-%m-%d)]" 2>/dev/null || echo "Git commit skipped (no changes)"
    echo "✅ Git committed."
else
    echo "\n[3/3] 跳过Git提交（未初始化）"
fi

echo "\n=== quant-ratio ${THEME} 投资看板刷新完成 ==="
