#!/usr/bin/env python3
"""
讀回 hcl-verse-RAG 產生的 external_contacts.xlsx（人工填好 canonical_name 的列），
回填三個地方：
  1. email_mapping（PostgreSQL，upsert，不會清掉手動加的列）
  2. Qdrant verse_emails collection（set_payload 更新 from_name）
  3. Hindsight EID bank（先 get_document 讀回舊 tags/metadata，合併新姓名後
     重新 retain——retain() 是整段覆蓋不是 merge，沒帶到的 tags/metadata 會消失）
最後把 external_contacts_state.json 對應的聯絡人標成 confirmed=true，
下次 hcl-verse-RAG 產生 Excel 時就不會再列出來。

用法：
    python update_external_contacts.py [xlsx_path]
"""
import os, sys, json, warnings
warnings.filterwarnings('ignore')

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import psycopg2
import requests
from openpyxl import load_workbook
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

# 共用 hcl-verse-RAG 既有的 tracker 模組（state 檔案讀寫），不重複實作
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hcl-verse-RAG"))
from external_contacts_tracker import load_state, save_state

QDRANT_URL   = os.environ.get("QDRANT_URL", "http://10.11.1.40:6333")
HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")
COLLECTION   = "verse_emails"
XLSX_PATH    = os.path.expanduser("~/verse-export/external_contacts.xlsx")

PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB   = os.environ.get("PG_DB", "")
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")


class HindsightClient:
    def __init__(self, url):
        self.url = url
        resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "update-external-contacts", "version": "1.0"}},
        })
        self.session_id = resp.headers.get("mcp-session-id")

    def _call(self, name, arguments):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, headers={"mcp-session-id": self.session_id}, timeout=30)
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                return json.loads(line[5:])
        return {}

    def get_document(self, document_id, bank_id="EID"):
        """讀回既有記錄的 tags/document_metadata，重新 retain 前一定要先呼叫這個，
        否則 retain() 整段覆蓋會把沒帶到的 tags/metadata 洗掉。"""
        result = self._call("get_document", {"document_id": document_id, "bank_id": bank_id})
        text = result.get("result", {}).get("content", [{}])[0].get("text", "")
        try:
            return json.loads(text)
        except Exception:
            return None

    def retain(self, content, document_id, timestamp, metadata, context, tags=None, bank_id="EID"):
        return self._call("retain", {
            "content": content, "document_id": document_id, "timestamp": timestamp,
            "metadata": metadata, "context": context, "tags": tags, "bank_id": bank_id,
        })


def upsert_email_mapping(email, name):
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO email_mapping (name, email) VALUES (%s, %s) "
                "ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name",
                (name, email),
            )
        conn.commit()
    finally:
        conn.close()


def read_confirmed_rows(xlsx_path):
    """讀 Excel，只取 canonical_name 有填的列，回傳 [(email, canonical_name), ...]。"""
    wb = load_workbook(xlsx_path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    email_idx = header.index("email")
    name_idx = header.index("canonical_name")
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        email = row[email_idx] if email_idx < len(row) else None
        name = row[name_idx] if name_idx < len(row) else None
        if email and name and str(name).strip():
            rows.append((str(email).strip().lower(), str(name).strip()))
    return rows


def backfill_one(qdrant, hindsight, email, canonical_name):
    """回填單一聯絡人在 Qdrant/Hindsight 裡的所有記錄，回傳實際更新的筆數。"""
    points, _ = qdrant.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(must=[FieldCondition(key="from_email", match=MatchValue(value=email))]),
        limit=1000, with_payload=True,
    )
    if not points:
        print(f"  ↷ Qdrant 查無 {email} 相關資料，跳過（可能當初 RAG 那步失敗過，不是錯誤）")
        return 0

    updated = 0
    for p in points:
        unid = p.payload.get("unid")
        if not unid:
            continue

        qdrant.set_payload(collection_name=COLLECTION, payload={"from_name": canonical_name}, points=[p.id])

        doc = hindsight.get_document(unid)
        if doc is None:
            print(f"  ✗ Hindsight 查無 unid={unid}，跳過（可能當初 retain 那步失敗過，不是錯誤）")
            continue

        old_tags = doc.get("tags") or ["mail"]
        old_metadata = dict(doc.get("document_metadata") or {})
        old_metadata["from_name"] = canonical_name
        old_metadata["from_email"] = email

        subject = old_metadata.get("subject", p.payload.get("subject", ""))
        sent_date = old_metadata.get("sent_date", p.payload.get("sent_date", ""))
        body = p.payload.get("body", "")

        content = (
            f"主旨：{subject}\n"
            f"寄件者：{canonical_name} <{email}>\n"
            f"日期：{sent_date}\n\n{body}"
        )
        result = hindsight.retain(
            content=content, document_id=unid, timestamp=sent_date,
            metadata=old_metadata, tags=old_tags,
            context=f"HCL Verse 信件：主旨「{subject}」，寄件者 {canonical_name}",
        )
        result_text = result.get("result", {}).get("content", [{}])[0].get("text", "")
        if "validation error" in result_text.lower():
            print(f"  ✗ Hindsight retain 被拒絕（unid={unid}）：{result_text[:200]}")
            continue
        updated += 1

    return updated


def main():
    xlsx_path = sys.argv[1] if len(sys.argv) > 1 else XLSX_PATH
    if not os.path.exists(xlsx_path):
        print(f"找不到 {xlsx_path}")
        return

    rows = read_confirmed_rows(xlsx_path)
    if not rows:
        print("Excel 裡沒有已填 canonical_name 的列，沒有要回填的")
        return

    qdrant = QdrantClient(url=QDRANT_URL)
    hindsight = HindsightClient(HINDSIGHT_URL)
    state = load_state()

    summary = []
    for email, canonical_name in rows:
        print(f"處理 {email} -> {canonical_name}")
        upsert_email_mapping(email, canonical_name)
        n = backfill_one(qdrant, hindsight, email, canonical_name)
        if email in state:
            state[email]["confirmed"] = True
        summary.append({"email": email, "canonical_name": canonical_name, "unids_updated": n})
        print(f"  ✓ 回填 {n} 筆")

    save_state(state)

    print("\n=== 完成 ===")
    for s in summary:
        print(f"  {s['email']} -> {s['canonical_name']}：{s['unids_updated']} 筆")


if __name__ == "__main__":
    main()
