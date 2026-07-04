#!/usr/bin/env python3
"""
HCL Notes 簽核記錄寫入 Hindsight — REST API 直連（不透過 MCP）

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
    parser.add_argument("--date", required=True, help="處理日期，例如 2026-07-03，用來組共用 tag 與 fallback document_id")
    parser.add_argument("--tag", action="append", default=None, help="共用標籤，可重複指定")
    parser.add_argument("--content-file", help="單筆模式：內容檔案路徑（UTF-8 純文字/Markdown）")
    parser.add_argument("--items-file", help="多筆模式：JSON 陣列檔案路徑，每筆可帶自己的 timestamp")
    args = parser.parse_args()

    if not args.content_file and not args.items_file:
        print("  ✗ 必須指定 --content-file 或 --items-file 其中之一", flush=True)
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


if __name__ == "__main__":
    main()
