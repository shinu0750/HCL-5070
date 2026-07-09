#!/usr/bin/env python3
"""
HCL Notes 簽核記錄寫入 Hindsight — REST API 直連（不透過 MCP）
寫入成功後可選擇再通知 Google Chat（透過 n8n webhook 轉發）。

Hindsight 是自架在 WSL Docker 裡的服務（container: hindsight），
Windows 端可直接連 http://localhost:8888，無需登入驗證。

兩種用法：

1. 單筆內容（整批摘要當一筆 memory，timestamp 預設為今天）：
   python hcl_write_hindsight.py --date 2026-07-03 --content-file summary.md

2. 多筆內容，各自帶正確的實際發生時間（推薦，Hindsight 時間軸才會準確）：
   python hcl_write_hindsight.py --date 2026-07-03 --items-file items.json

   items.json 格式：
   [
     {"content": "...", "timestamp": "2026-07-02T14:30:00", "tags": ["..."], "document_id": "..."},
     ...
   ]
   每筆的 tags/document_id 若省略，會自動補上 --tag 指定的共用標籤與依 content 雜湊產生的 document_id。

寫完 Hindsight 後通知 Google Chat（選用，加 --notify-file + --space）：
   python hcl_write_hindsight.py --date 2026-07-03 --items-file items.json --notify-file summary_table.md --space h2YgpyAAAAE

   Hindsight 寫入全部成功後，會把 --notify-file 的內容 POST 到 n8n workflow
   「[HCL] 簽核完成通知 -> Google Chat」的 webhook，轉發到指定 space 的 Google Chat。
   若 Hindsight 寫入失敗則不會發送通知。

   ⚠️ --space 是必填（用 --notify-file 時）：n8n 端沒有預設值，每個使用者（包括自己）都要
   明確帶自己的 space ID，不依賴任何隱式 fallback。代簽別人帳號時記得換成對方的 space
   （見 hcl-notes-approval SKILL.md 的「使用者對照表」）：
   python hcl_write_hindsight.py --date 2026-07-03 --items-file items.json --notify-file summary_table.md --space 8DyTYKAAAAE

3. 純通知（不寫 Hindsight，用於密碼錯誤等告警場景）：
   python hcl_write_hindsight.py --notify-only --notify-file alert.md --space h2YgpyAAAAE
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HINDSIGHT_BASE = "http://localhost:8888"
N8N_NOTIFY_WEBHOOK = "http://10.11.1.59:5678/webhook/hcl-approval-notify"


def notify_google_chat(text, space, webhook=N8N_NOTIFY_WEBHOOK):
    """把 text POST 給 n8n webhook，轉發到 Google Chat。space 必填——
    n8n 端的 Google Chat 節點沒有預設值，沒帶 space 會直接在 Google Chat 那邊失敗。"""
    payload = {"text": text, "space": space if space.startswith("spaces/") else f"spaces/{space}"}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def retain_async(bank_id, items):
    """
    非同步送出：每筆 memory 都要跑 LLM 事實萃取，實測單筆約 20~30 秒，
    多筆同步送很容易超過 HTTP timeout。用 async=true 立即拿回 operation_ids，
    再輪詢 operations 端點直到全部完成。
    """
    url = f"{HINDSIGHT_BASE}/v1/default/banks/{bank_id}/memories"
    body = {"items": items, "async": True}
    result = _post(url, body, timeout=30)
    op_ids = result.get("operation_ids") or ([result["operation_id"]] if result.get("operation_id") else [])
    if not op_ids:
        return result

    print(f"  已排入佇列，operation_ids={op_ids}，開始輪詢...", flush=True)
    pending = set(op_ids)
    statuses = {}
    start = time.time()
    while pending and time.time() - start < 600:
        for op_id in list(pending):
            op_url = f"{HINDSIGHT_BASE}/v1/default/banks/{bank_id}/operations/{op_id}"
            try:
                op = _get(op_url, timeout=15)
            except urllib.error.URLError:
                continue
            status = op.get("status")
            if status in ("completed", "failed", "cancelled", "not_found"):
                statuses[op_id] = op
                pending.discard(op_id)
                print(f"    {op_id} -> {status}", flush=True)
        if pending:
            time.sleep(5)

    if pending:
        print(f"  ⚠️ 逾時仍未完成：{pending}", flush=True)
    return {"result": result, "operations": statuses}


def main():
    parser = argparse.ArgumentParser(description="寫入 HCL 簽核記錄到 Hindsight")
    parser.add_argument("--bank", default="EID", help="Hindsight bank_id（預設 EID）")
    parser.add_argument("--date", help="處理日期，例如 2026-07-03，用來組共用 tag 與 fallback document_id（--notify-only 時不需要）")
    parser.add_argument("--tag", action="append", default=None, help="共用標籤，可重複指定")
    parser.add_argument("--content-file", help="單筆模式：內容檔案路徑（UTF-8 純文字/Markdown）")
    parser.add_argument("--items-file", help="多筆模式：JSON 陣列檔案路徑，每筆可帶自己的 timestamp")
    parser.add_argument("--notify-file", help="把此檔案內容原封不動發送到 Google Chat（預設須等 Hindsight 寫入成功；配合 --notify-only 可略過 Hindsight 直接發送）")
    parser.add_argument("--notify-only", action="store_true", help="只發送 Google Chat 通知，不寫入 Hindsight（用於密碼錯誤等告警場景，需搭配 --notify-file + --space）")
    parser.add_argument("--space", help="通知目標的 Google Chat space ID（例如 8DyTYKAAAAE 或 h2YgpyAAAAE）。用 --notify-file 時必填——n8n 端沒有預設值，每個使用者都要明確指定自己的 space")
    args = parser.parse_args()

    if args.notify_only:
        if not args.notify_file or not args.space:
            print("  ✗ --notify-only 必須同時指定 --notify-file 與 --space", flush=True)
            sys.exit(1)
        with open(args.notify_file, encoding="utf-8") as f:
            notify_text = f.read()
        print(f"  通知 Google Chat（webhook: {N8N_NOTIFY_WEBHOOK}，space: {args.space}）...", flush=True)
        try:
            notify_result = notify_google_chat(notify_text, space=args.space)
            print(f"    -> {notify_result}", flush=True)
        except urllib.error.URLError as e:
            print(f"  ✗ Google Chat 通知失敗：{e}", flush=True)
            sys.exit(1)
        return

    if not args.date:
        print("  ✗ 必須指定 --date", flush=True)
        sys.exit(1)

    if not args.content_file and not args.items_file:
        print("  ✗ 必須指定 --content-file 或 --items-file 其中之一", flush=True)
        sys.exit(1)

    if args.notify_file and not args.space:
        print("  ✗ 使用 --notify-file 時必須指定 --space（n8n 端沒有預設值，每個使用者都要明確指定）", flush=True)
        sys.exit(1)

    base_tags = ["hcl-approval", args.date]
    extra_tags = [t for t in (args.tag or []) if t not in base_tags]

    if args.items_file:
        with open(args.items_file, encoding="utf-8") as f:
            raw_items = json.load(f)
        items = []
        for it in raw_items:
            tags = base_tags + extra_tags + [t for t in it.get("tags", []) if t not in base_tags + extra_tags]
            document_id = it.get("document_id") or (
                "hcl-approval-" + hashlib.md5(it["content"].encode("utf-8")).hexdigest()[:12]
            )
            item = {"content": it["content"], "tags": tags, "document_id": document_id}
            if it.get("timestamp"):
                item["timestamp"] = it["timestamp"]
            items.append(item)
    else:
        with open(args.content_file, encoding="utf-8") as f:
            content = f.read()
        items = [{
            "content": content,
            "tags": base_tags + extra_tags,
            "document_id": f"hcl-approval-{args.date}",
        }]

    print(f"  寫入 bank={args.bank}，共 {len(items)} 筆", flush=True)
    for it in items:
        print(f"    - {it['document_id']} @ {it.get('timestamp', '(now)')}", flush=True)

    try:
        result = retain_async(args.bank, items)
    except urllib.error.URLError as e:
        print(f"  ✗ 連線失敗：{e}", flush=True)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

    operations = result.get("operations", {}) if isinstance(result, dict) else {}
    all_ok = all(op.get("status") == "completed" for op in operations.values()) if operations else True

    if args.notify_file:
        if not all_ok:
            print("  ⚠️ Hindsight 寫入未全部成功，略過 Google Chat 通知", flush=True)
        else:
            with open(args.notify_file, encoding="utf-8") as f:
                notify_text = f.read()
            print(f"  通知 Google Chat（webhook: {N8N_NOTIFY_WEBHOOK}，space: {args.space}）...", flush=True)
            try:
                notify_result = notify_google_chat(notify_text, space=args.space)
                print(f"    -> {notify_result}", flush=True)
            except urllib.error.URLError as e:
                print(f"  ⚠️ Google Chat 通知失敗（Hindsight 已寫入成功，不影響資料）：{e}", flush=True)


if __name__ == "__main__":
    main()
