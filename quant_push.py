#!/usr/bin/env python3
"""投资看板 · Telegram 信号推送（18:30 日更 + T+1 终刷共用）

用法:
  python3 quant_push.py                       # 18:30 日更（run_quant_ratio.sh 调用）
  python3 quant_push.py --context "T+1 终刷"   # 沪市发布后的终刷

去重键 = quant_data.json 的 date（数据日 D） + sha1(信号行)。
→ 18:30 用「深市单口径」算出的信号与终刷用「沪深合计」算出的信号 hash 不同，
  各自最多推一次；同时数据日相同，不会把 D 的信号重复推两次。
无触发 / 信号文件为空 → 静默退出 0。
"""
import argparse
import hashlib
import json
import os
import sys

DASH = "/Users/jren/projects/hermes-dashboard"
SIGNAL_FILE = os.path.join(DASH, "quant_push_signal.txt")
DATA_FILE = os.path.join(DASH, "quant_data.json")
STATE = os.path.expanduser("~/.hermes/state/quant_push_state.json")
ENV = os.path.expanduser("~/.hermes/.env")
URL = "https://stranger971020.github.io/hermes-dashboard/dashboard_investment.html"


def push(context=""):
    """执行一次推送；无触发/已推过则静默。返回 0（不阻断上游）。"""
    try:
        signal = open(SIGNAL_FILE, encoding="utf-8").read().strip()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 读取信号文件失败: {e}")
        return 0
    if not signal or "无触发" in signal:
        print("（无触发，静默）")
        return 0

    try:
        data_date = json.load(open(DATA_FILE, encoding="utf-8")).get("date")
    except Exception:  # noqa: BLE001
        data_date = None
    sig_hash = hashlib.sha1(signal.encode()).hexdigest()[:12]

    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    if state.get("data_date") == data_date and state.get("sig_hash") == sig_hash:
        print(f"⏭  数据日 {data_date} 同一信号已推送过，跳过（防重复）")
        return 0

    token = None
    try:
        with open(ENV, "rb") as fh:
            for line in fh:
                if b"TELEGRAM_BOT_TOKEN" in line and not line.startswith(b"#"):
                    token = line.strip().split(b"=", 1)[1].decode("utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 读取 .env 失败: {e}")
    if not token:
        print("⚠️ 未找到 TELEGRAM_BOT_TOKEN，跳过推送")
        return 0

    head = "📊 投资看板 · 信号触发"
    if context:
        head += f"（{context}）"
    msg = head + "\n" + signal + "\n" + URL

    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": "8632265776", "text": msg}, timeout=20)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    if body.get("ok"):
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump({"data_date": data_date, "sig_hash": sig_hash,
                   "pushed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                   "message_id": body["result"]["message_id"]},
                  open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"✅ 信号已推送 Telegram (message_id={body['result']['message_id']})")
    else:
        print(f"⚠️ 推送失败: {r.text[:120]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", default="", help="附加在消息标题后的说明")
    args = ap.parse_args()
    return push(args.context)


if __name__ == "__main__":
    sys.exit(main())
