#!/usr/bin/env python3
"""
會議記錄 / 報價單附件 —— 批次處理（meeting_quote_upload.py 兩階段設計的第二階段）

verse_archive_pipeline.py 歸檔時只把符合關鍵字的 .pdf 附件存到
MEETING_QUOTE_STAGING_DIR（不同步跑 RAGAnything，避免拖慢歸檔），這支腳本掃描
那個資料夾，逐一送進 RAGAnything 解析、「會議記錄」類額外把全文寫進 Hindsight，
成功的搬到 done/ 子目錄（失敗的留在原地，重跑只補失敗的）。

用法：
    python meeting_quote_batch_process.py [--max N]

    --max N   本次最多處理 N 份（測試用，不加就處理全部待處理項目）

只處理「報價單」類（labels 裡含 quote 或跟 meeting 並存的）：純「會議記錄」類
（labels == {"meeting"}）一律略過不送 RAGAnything——最終目的地是 Hindsight，
應該直接讀取 PDF、抽取結論寫回 Hindsight，不用先跑本機 20-30 分鐘的 LLM 流程
再轉出（見 SKILL.md「已知缺口」與 meeting_notes_hindsight_workflow 這則
memory）。同時會用 md5 內容雜湊去重：同一份 PDF 被多封轉寄信各自附加、staged
成多筆 sidecar 時，只有第一筆真的送 RAGAnything，其餘內容相同的直接標記完成，
不重跑（同一份文件重複跑只會浪費本機 LLM 時間，不會讓知識庫更完整）。
"""
import os
import sys
import json
import shutil
import hashlib
import argparse
import tempfile
import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from meeting_quote_upload import (
    MEETING_QUOTE_STAGING_DIR, save_to_inputs, upload_to_raganything,
    find_parsed_markdown, write_meeting_to_hindsight,
)

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")
DONE_DIR = os.path.join(MEETING_QUOTE_STAGING_DIR, "done")
OUTPUT_FILE = os.path.join(tempfile.gettempdir(), "meeting_quote_batch_process_result.json")


class HindsightClient:
    def __init__(self, url):
        self.url = url
        resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "meeting-quote-batch", "version": "1.0"}},
        }, timeout=30)
        self.session_id = resp.headers.get("mcp-session-id")

    def retain(self, content, document_id, timestamp, metadata, context, tags=None, bank_id="EID"):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "retain", "arguments": {
                "content": content, "document_id": document_id, "timestamp": timestamp,
                "metadata": metadata, "context": context, "bank_id": bank_id, "tags": tags,
            }},
        }, headers={"mcp-session-id": self.session_id}, timeout=30)
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                return json.loads(line[5:])
        return {}


def _pdf_hash(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def find_pending():
    """列出 MEETING_QUOTE_STAGING_DIR 頂層（不含 done/）所有 .json sidecar +
    對應的 .pdf，缺一邊的跳過並記錄成 error。純「會議記錄」類（labels ==
    {"meeting"}）整個跳過，不列進待處理清單（回傳的第二個值是略過筆數，供呼叫端
    印出來）。同一份 PDF 內容（md5 相同）重複出現時，除了第一筆，其餘筆數標上
    `duplicate_of`，呼叫端據此直接標記完成、不重跑 RAGAnything。"""
    pending = []
    skipped_meeting = 0
    if not os.path.isdir(MEETING_QUOTE_STAGING_DIR):
        return pending, skipped_meeting
    seen_hashes = {}
    for entry in sorted(os.listdir(MEETING_QUOTE_STAGING_DIR)):
        if not entry.lower().endswith(".json"):
            continue
        stem = entry[:-len(".json")]
        json_path = os.path.join(MEETING_QUOTE_STAGING_DIR, entry)
        pdf_path = os.path.join(MEETING_QUOTE_STAGING_DIR, stem + ".pdf")
        if not os.path.isfile(pdf_path):
            pending.append({"json_path": json_path, "pdf_path": None, "error": "找不到對應的 .pdf"})
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            pending.append({"json_path": json_path, "pdf_path": pdf_path, "error": f"讀取 sidecar 失敗：{e}"})
            continue

        labels = set(meta.get("labels", []))
        if labels == {"meeting"}:
            skipped_meeting += 1
            continue

        item = {"json_path": json_path, "pdf_path": pdf_path, "meta": meta}
        try:
            h = _pdf_hash(pdf_path)
            if h in seen_hashes:
                item["duplicate_of"] = seen_hashes[h]
            else:
                seen_hashes[h] = meta.get("original_name", stem)
        except Exception:
            pass  # 算不出雜湊就當非重複處理，不影響正確性，只是少了去重優化
        pending.append(item)
    return pending, skipped_meeting


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None, help="本次最多處理幾份（測試用）")
    return ap.parse_args()


def main():
    args = parse_args()
    hindsight = HindsightClient(HINDSIGHT_URL)
    items, skipped_meeting = find_pending()
    total_pending = len(items)
    if args.max:
        items = items[:args.max]
    print(f"📄 {MEETING_QUOTE_STAGING_DIR} 待處理 {total_pending} 份"
          + (f"（另有 {skipped_meeting} 份純會議記錄類已略過，改走直接讀取+寫回 Hindsight，不進 RAGAnything）" if skipped_meeting else "")
          + (f"，本次限制只跑 {len(items)} 份" if args.max else ""))
    if not items:
        print("🎉 沒有待處理的附件")
        json.dump({"total": 0, "succeeded": 0, "failed": 0, "results": []},
                   open(OUTPUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return

    os.makedirs(DONE_DIR, exist_ok=True)
    succeeded, failed, results = 0, 0, []

    for i, item in enumerate(items, 1):
        if "meta" not in item:
            failed += 1
            results.append({"file": item["json_path"], "status": "failed", "detail": item["error"]})
            print(f"❌ [{i}/{len(items)}] {os.path.basename(item['json_path'])} — {item['error']}")
            continue

        meta = item["meta"]
        unid = meta.get("unid", "")
        name = meta.get("original_name", os.path.basename(item["pdf_path"]))
        subject = meta.get("subject", "")
        sender_name = meta.get("sender_name", "")
        sent_date = meta.get("sent_date", "")
        labels = meta.get("labels", [])

        if item.get("duplicate_of"):
            # 內容跟已經處理過的另一份 PDF 完全相同（md5 一致，通常是同一份文件被
            # 多封轉寄信各自附加），共用知識庫裡已經有這份內容了，不用重跑一次
            # 白白多花本機 LLM 時間
            succeeded += 1
            for src in (item["pdf_path"], item["json_path"]):
                shutil.move(src, os.path.join(DONE_DIR, os.path.basename(src)))
            print(f"✅ [{i}/{len(items)}] {name[:50]} — 與「{item['duplicate_of'][:40]}」內容相同，略過重跑")
            results.append({"file": name, "unid": unid, "labels": labels, "status": "succeeded",
                             "skip_reason": f"duplicate_of={item['duplicate_of']}"})
            continue

        rec = {"file": name, "unid": unid, "labels": labels}
        try:
            with open(item["pdf_path"], "rb") as f:
                data = f.read()
            _, fname = save_to_inputs(unid, name, data)
            ok, detail = upload_to_raganything(fname)
            rec["raganything_ok"] = ok
            if not ok:
                rec["error"] = detail[:500]
        except Exception as e:
            rec["raganything_ok"] = False
            rec["error"] = str(e)
            ok = False

        hindsight_ok = None
        if ok and "meeting" in labels:
            try:
                md_text = find_parsed_markdown(fname)
                if md_text:
                    result = write_meeting_to_hindsight(
                        hindsight, unid, name, md_text, subject, sender_name, sent_date)
                    result_text = result.get("result", {}).get("content", [{}])[0].get("text", "")
                    hindsight_ok = "validation error" not in result_text.lower()
                    if not hindsight_ok:
                        rec["error"] = result_text[:500]
                else:
                    hindsight_ok = False
                    rec["error"] = "找不到解析後的 markdown（output 目錄沒對應檔案）"
            except Exception as e:
                hindsight_ok = False
                rec["error"] = str(e)
        rec["hindsight_ok"] = hindsight_ok

        # 「會議記錄」類要 RAGAnything+Hindsight 都成功才算過；「報價單」類只看
        # RAGAnything（不寫 Hindsight，hindsight_ok 維持 None）
        is_ok = ok and (hindsight_ok is not False)
        if is_ok:
            succeeded += 1
            rec["status"] = "succeeded"
            for src in (item["pdf_path"], item["json_path"]):
                dest = os.path.join(DONE_DIR, os.path.basename(src))
                shutil.move(src, dest)
            flag_extra = f", Hindsight全文{'✓' if hindsight_ok else '—'}"
            print(f"✅ [{i}/{len(items)}] {name[:50]}{flag_extra}")
        else:
            failed += 1
            rec["status"] = "failed"
            print(f"❌ [{i}/{len(items)}] {name[:50]} — {rec.get('error', '')[:80]}")

        results.append(rec)

    print(f"\n✓ 完成：成功 {succeeded}、失敗 {failed}（失敗的留在原地，重跑只補失敗的）")
    json.dump({"total": len(items), "succeeded": succeeded, "failed": failed, "results": results},
               open(OUTPUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  結果已寫入 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
