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
    git add collect_quant_ratio.py quant_ratio_render.py verify_quant.py run_quant_ratio.sh \
        dashboard_investment.html quant_data.json \
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
if [ -f "${SIGNAL_FILE}" ] && ! grep -q "无触发" "${SIGNAL_FILE}"; then
    ${PY} - <<'PYEOF'
import datetime
import hashlib
import json
import os

import requests

STATE = os.path.expanduser("~/.hermes/state/quant_push_state.json")
ENV = os.path.expanduser("~/.hermes/.env")
URL = "https://stranger971020.github.io/hermes-dashboard/dashboard_investment.html"
today = datetime.date.today().isoformat()

with open("/Users/jren/projects/hermes-dashboard/quant_push_signal.txt") as f:
    push_line = f.read().strip()
sig_hash = hashlib.sha1(push_line.encode()).hexdigest()[:12]

state = {}
if os.path.exists(STATE):
    try:
        state = json.load(open(STATE))
    except Exception:  # noqa: BLE001
        state = {}
if state.get("date") == today and state.get("sig_hash") == sig_hash:
    print("⏭  同一信号今日已推送过，跳过（防刷屏）")
    raise SystemExit(0)

TOKEN = None
with open(ENV, "rb") as f:
    for line in f:
        if b"TELEGRAM_BOT_TOKEN" in line and not line.startswith(b"#"):
            TOKEN = line.strip().split(b"=", 1)[1].decode("utf-8")
if not TOKEN:
    print("⚠️ 未找到 TELEGRAM_BOT_TOKEN，跳过推送")
    raise SystemExit(0)

msg = "📊 投资看板 · 信号触发\n" + push_line + "\n" + URL
r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": "8632265776", "text": msg}, timeout=20)
try:
    body = r.json()
except Exception:  # noqa: BLE001
    body = {}
if body.get("ok"):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"date": today, "sig_hash": sig_hash,
               "message_id": body["result"]["message_id"]}, open(STATE, "w"))
    print(f"✅ 信号已推送 Telegram (message_id={body['result']['message_id']})")
else:
    print(f"⚠️ 推送失败: {r.text[:120]}")
PYEOF
else
    echo "（无触发，静默）"
fi

echo ""
echo "=== quant-ratio ${THEME} 投资看板刷新完成 ==="
