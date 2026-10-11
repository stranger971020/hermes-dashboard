#!/bin/bash
# run.sh — 刷新看板：collect → render → 入 vault(跨设备) → 提交
set -uo pipefail
DIR="$HOME/projects/hermes-dashboard"
VAULT="$HOME/Documents/Obsidian Vault/dashboard"
MIRROR="$HOME/hermes-agent/data/vault_mirror/dashboard"
PY=/Users/jren/miniforge3/bin/python3
TS="[$(date '+%F %T')]"

cd "$DIR" || exit 1
$PY collect.py >/dev/null 2>&1 || { echo "$TS ERROR: collect 失败"; exit 1; }
$PY render.py --public  >/dev/null 2>&1 || { echo "$TS ERROR: render(public) 失败"; exit 1; }
$PY render.py --private >/dev/null 2>&1 || { echo "$TS ERROR: render(private) 失败"; exit 1; }

# 1) 写入 vault + mirror（跨设备；完整版只进 vault，不进公开仓）
mkdir -p "$VAULT" "$MIRROR"
cp index.html index-private.html data.json lookup.json "$VAULT"/ 2>/dev/null
cp index.html index-private.html "$MIRROR"/ 2>/dev/null

# 2) 提交到看板仓库（有变化才提交）
git add -A
if git diff --cached --quiet; then
  echo "$TS no changes"
else
  git commit -q -m "dashboard: $(date '+%F %T')" || true
  if git push -q origin main 2>/dev/null; then echo "$TS OK pushed $(git rev-parse --short HEAD)"
  else echo "$TS WARN: push 失败（看板已在本地/vault，不影响查看）"; fi
fi
