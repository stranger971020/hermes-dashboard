# Hermes 监控看板

自更新看板，三个 tab：
- **定时任务** — Hermes cron + 系统 crontab + launchd（排程/上次运行/下次运行/状态）
- **Ad-hoc 任务** — vault 历史任务按主题聚类，可勾选生成归档指令
- **盘点 Monitor** — 磁盘/备份/服务/vault/技能 健康快照

## 用法
```bash
./run.sh          # 刷新（collect → render → 写 vault → 提交）
```
或直接：`python3 collect.py && python3 render.py`

产物：`index.html`（自包含），同时写入 `Obsidian Vault/dashboard/`。
