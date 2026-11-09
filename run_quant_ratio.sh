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
    echo "[3/4] 提交Git..."
    cd "${DASHBOARD_PATH}"
    git add collect_quant_ratio.py quant_ratio_render.py run_quant_ratio.sh dashboard_investment.html quant_data.json "history/${THEME}_*.html" "history/${THEME}_*.json" 2>/dev/null || true
    git commit -m "quant-ratio: update ${THEME} dashboard [$(date +%Y-%m-%d)]" 2>/dev/null || echo "Git commit skipped (no changes)"
    git push origin main 2>/dev/null || echo "Git push failed (will retry next run)"
    echo "✅ Git committed + pushed."
else
    echo ""
    echo "[3/4] 跳过Git提交（未初始化）"
fi

# 4. 投资信号触发 → Telegram 推送（无触发则静默，不占阅读槽位）
echo ""
echo "[4/4] 检查投资信号..."
SIGNAL_FILE="${DASHBOARD_PATH}/quant_push_signal.txt"
if [ -f "${SIGNAL_FILE}" ] && ! grep -q "无触发" "${SIGNAL_FILE}"; then
    ${PY} - <<'PYEOF'
import requests, os
with open("/Users/jren/.hermes/.env", "rb") as f:
    for line in f:
        if b'TELEGRAM_BOT_TOKEN' in line and not line.startswith(b'#'):
            TOKEN = line.strip().split(b'=', 1)[1].decode('utf-8')
with open("/Users/jren/projects/hermes-dashboard/quant_push_signal.txt") as f:
    push_line = f.read().strip()
msg = ("📊 投资看板 · 信号触发\n" + push_line +
       "\nhttps://stranger971020.github.io/hermes-dashboard/dashboard_investment.html")
r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": "8632265776", "text": msg}, timeout=20)
ok = r.json().get("ok")
print(("✅ 信号已推送 Telegram (message_id=%s)" % r.json()["result"]["message_id"]) if ok else f"⚠️ 推送失败: {r.text[:120]}")
PYEOF
else
    echo "（无触发，静默）"
fi

echo ""
echo "=== quant-ratio ${THEME} 投资看板刷新完成 ==="
