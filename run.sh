#!/bin/bash
# run.sh — 刷新看板：collect → render → 入 vault(跨设备) → 提交
set -uo pipefail
DIR="$HOME/projects/hermes-dashboard"
VAULT="$HOME/Documents/Obsidian Vault/dashboard"
MIRROR="$HOME/hermes-agent/data/vault_mirror/dashboard"
PY=/Users/jren/miniforge3/bin/python3
TS="[$(date '+%F %T')]"

FORCE=""; [ "${1:-}" = "--force" ] && FORCE=1
PY=/Users/jren/miniforge3/bin/python3

# ── 0) 中枢检查（内部按到期规则调度；替代 4 个独立检查 cron）──
CHK=$($PY "$HOME/projects/hermes-dashboard/checks.py" --due 2>&1); CHK_RC=$?
echo "$TS $CHK" >> "$HOME/.hermes/logs/hub_checks.log"
if [ "$CHK_RC" -ne 0 ]; then
  $PY "$HOME/.hermes/scripts/dashboard_push.py" --checks-alert >/dev/null 2>&1 || true
fi

# ── 指纹守卫：任务/回执/cron/检查结果 无变化则直接退出 ──
FP=$($PY - <<'PYEOF'
import os, hashlib, subprocess
H = os.path.expanduser("~"); h = hashlib.md5()
for sub in ["Documents/Obsidian Vault/tasks", "Documents/Obsidian Vault/responses"]:
    d = os.path.join(H, sub)
    try: ents = sorted(os.listdir(d))
    except Exception: ents = []
    h.update(",".join(ents).encode())
    for f in ents:
        try: h.update(str(int(os.path.getmtime(os.path.join(d, f)))).encode())
        except Exception: pass
try: h.update(str(int(os.path.getmtime(os.path.join(H, ".hermes/cron/jobs.json")))).encode())
except Exception: pass
try: h.update(str(int(os.path.getmtime(os.path.join(H, ".hermes/state/hub_checks.json")))).encode())
except Exception: pass
try: h.update(str(int(os.path.getmtime(os.path.join(H, "Documents/Obsidian Vault/logs/skill-changelog.md")))).encode())
except Exception: pass
try: h.update(str(int(os.path.getmtime(os.path.join(H, ".hermes/config.yaml")))).encode())
except Exception: pass
try: h.update(subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout.encode())
except Exception: pass
print(h.hexdigest())
PYEOF
)
STATE="$DIR/state.json"
PREV=$($PY -c "import json;print(json.load(open('$STATE')).get('fp',''))" 2>/dev/null || echo "")
if [ -z "$FORCE" ] && [ "$FP" = "$PREV" ]; then
  echo "$TS no change (fp=${FP:0:8})"; exit 0
fi

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

# 3) 记录指纹（仅在成功刷新后）
echo "{\"fp\":\"$FP\",\"at\":\"$(date '+%F %T')\"}" > "$STATE"
