#!/usr/bin/env python3
"""
Gmail 全信箱 → Hindsight + Qdrant backfill
每封信：document_id = hash(from|subject|date)，idempotent，可重跑/斷點續跑

用法：
    python3 gmail_backfill.py [--max N] [--label LABEL] [--dry-run] [--reset]

選項：
    --max N       只處理前 N 封（預設：全部）
    --label LABEL 只處理有此 Gmail 標籤的信（預設：全部）
    --dry-run     只印不寫入
    --reset       清除進度檔，從頭開始
"""
import os, sys, json, hashlib, re, base64, argparse, time
import requests
from pathlib import Path
from email.utils import parsedate_to_datetime

_env = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env):
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from openai import OpenAI

sys.path.insert(0, os.path.expanduser("~/.claude/skills/hcl-verse-RAG"))
from project_keywords import match_projects

# ── 設定 ──────────────────────────────────────────────────────────────────────
GMAIL_DIR        = "/Users/shuhsing/Documents/eml to gamil"
CREDENTIALS_FILE = os.path.join(GMAIL_DIR, "credentials.json")
TOKEN_FILE       = os.path.join(GMAIL_DIR, "token.json")
SCOPES           = ["https://www.googleapis.com/auth/gmail.modify"]
HINDSIGHT_URL    = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")
QDRANT_URL       = os.environ.get("QDRANT_URL",    "http://10.11.1.40:6333")
OPENAI_KEY       = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "http://localhost:8081/v1")
EMBEDDING_MODEL    = os.environ.get("EMBEDDING_MODEL",    "jina-embed")
COLLECTION       = "verse_emails"
VECTOR_SIZE      = 2048
PROGRESS_FILE    = os.path.expanduser("~/.claude/skills/hcl-verse-RAG/backfill_progress.json")
PAGE_SIZE        = 100
SAVE_EVERY       = 50   # 每 N 封存一次進度


# ── Hindsight HTTP client ─────────────────────────────────────────────────────
class HindsightClient:
    def __init__(self, url):
        self.url = url
        self.session_id = None
        self._init()

    def _init(self):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gmail-backfill", "version": "1.0"},
            }
        })
        self.session_id = resp.headers.get("mcp-session-id")
        if not self.session_id:
            raise RuntimeError(f"Hindsight 初始化失敗：{resp.text[:200]}")

    def _call(self, name, arguments):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, headers={"mcp-session-id": self.session_id}, timeout=30)
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise RuntimeError(f"無法解析回應：{resp.text[:200]}")

    def retain(self, content, document_id, timestamp, tags, metadata, context, bank_id="shuhsing"):
        return self._call("retain", {
            "content":     content,
            "document_id": document_id,
            "timestamp":   timestamp,
            "tags":        tags,
            "metadata":    metadata,
            "context":     context,
            "bank_id":     bank_id,
        })


# ── 工具函式 ──────────────────────────────────────────────────────────────────
def make_doc_id(from_, subject, date_raw):
    return hashlib.md5(f"{from_}|{subject}|{date_raw}".encode()).hexdigest()

def make_thread_id(subject):
    normalized = re.sub(
        r'^(回覆[:：]\s*|RE[:：]\s*|FW[:：]\s*|Fwd[:：]\s*)+',
        '', subject, flags=re.IGNORECASE
    ).strip()
    return hashlib.md5(normalized.encode()).hexdigest()

def id_to_uuid(h):
    h = h.ljust(32, "0")[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""

def normalize_date(date_raw):
    try:
        return parsedate_to_datetime(date_raw).isoformat()
    except Exception:
        return date_raw or ""

def extract_plain(payload):
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = extract_plain(part)
            if text:
                return text
    return ""

def extract_attachments(payload):
    """回傳真實附件檔名列表（排除 inline 裝飾圖片）"""
    results = []
    filename = payload.get("filename", "")
    if filename:
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        disposition = headers.get("content-disposition", "")
        if disposition.lower().startswith("attachment"):
            results.append(filename)
    for part in payload.get("parts", []):
        results.extend(extract_attachments(part))
    return results

def get_embedding(text, client):
    res = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text[:8000],
    )
    return res.data[0].embedding

def ensure_collection(qdrant):
    names = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in names:
        qdrant.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"建立 Qdrant collection: {COLLECTION}")

def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done_ids": [], "page_token": None,
            "stats": {"retain_ok": 0, "retain_fail": 0, "qdrant_ok": 0, "skipped": 0, "total": 0}}

def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max",      type=int, default=0,  help="最多處理幾封（0=全部）")
    ap.add_argument("--label",    default="",           help="只處理有此標籤的信")
    ap.add_argument("--dry-run",  action="store_true",  help="只印不寫入")
    ap.add_argument("--reset",    action="store_true",  help="清除進度，從頭開始")
    args = ap.parse_args()

    if args.reset and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("進度已重置")

    print("初始化 Qdrant / Hindsight / OpenAI...")
    qdrant       = QdrantClient(url=QDRANT_URL)
    openai_cli   = OpenAI(api_key=OPENAI_KEY or "local-no-key-needed", base_url=EMBEDDING_API_BASE)
    hindsight    = HindsightClient(HINDSIGHT_URL) if not args.dry_run else None
    ensure_collection(qdrant)

    creds   = authenticate()
    service = build("gmail", "v1", credentials=creds)

    progress   = load_progress()
    done_ids   = set(progress["done_ids"])
    page_token = progress.get("page_token")
    stats      = progress["stats"]
    processed  = 0

    print(f"Hindsight session: {hindsight.session_id if hindsight else '（dry-run）'}")
    print(f"已完成：{len(done_ids)} 封，繼續處理...\n")

    while True:
        kwargs = {"userId": "me", "maxResults": PAGE_SIZE}
        if page_token:
            kwargs["pageToken"] = page_token
        if args.label:
            kwargs["q"] = f"label:{args.label}"

        result   = service.users().messages().list(**kwargs).execute()
        messages = result.get("messages", [])
        page_token = result.get("nextPageToken")

        if not messages:
            break

        for msg_ref in messages:
            msg_id = msg_ref["id"]

            if msg_id in done_ids:
                stats["skipped"] += 1
                continue

            # ── 抓完整信件 ──
            try:
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
            except Exception as e:
                print(f"  ⚠ 抓取失敗 {msg_id}: {e}")
                continue

            payload   = msg.get("payload", {})
            headers   = payload.get("headers", [])
            subject   = get_header(headers, "Subject") or "(無主旨)"
            from_     = get_header(headers, "From") or ""
            date_raw  = get_header(headers, "Date") or ""
            date_iso  = normalize_date(date_raw)
            label_ids = msg.get("labelIds", [])
            gmail_tid = msg.get("threadId", "")

            body        = extract_plain(payload).strip() or msg.get("snippet", "")
            attachments = extract_attachments(payload)

            doc_id    = make_doc_id(from_, subject, date_raw)
            thread_id = make_thread_id(subject)
            projs     = match_projects(subject, body[:500])
            tags      = ["source:verse"] + [f"proj:{p}" for p in projs]
            content   = f"主旨：{subject}\n寄件者：{from_}\n日期：{date_iso}\n\n{body}"
            context   = f"HCL Verse 信件：主旨「{subject}」，寄件者 {from_}"
            metadata  = {
                "subject":         subject,
                "from":            from_,
                "thread_id":       thread_id,
                "gmail_id":        msg_id,
                "gmail_thread_id": gmail_tid,
                "label_ids":       ",".join(label_ids),
                "sent_date":       date_iso,
                "has_attachments": "true" if attachments else "false",
                "attachments":     ",".join(attachments) if attachments else "",
            }

            if args.dry_run:
                print(f"[dry] {subject[:50]} | projs:{projs} | tags:{tags}")
                done_ids.add(msg_id)
                processed += 1
                stats["total"] += 1
                continue

            # ── Hindsight retain ──
            try:
                hindsight.retain(
                    content=content, document_id=doc_id,
                    timestamp=date_iso, tags=tags,
                    metadata=metadata, context=context,
                )
                stats["retain_ok"] += 1
            except Exception as e:
                print(f"  ⚠ Hindsight [{subject[:40]}]: {e}")
                stats["retain_fail"] += 1

            # ── Qdrant embed ──
            try:
                vec = get_embedding(f"{subject} {body[:3000]}", openai_cli)
                qdrant.upsert(
                    collection_name=COLLECTION,
                    points=[PointStruct(
                        id=id_to_uuid(doc_id),
                        vector=vec,
                        payload={
                            "id": doc_id, "subject": subject,
                            "body": body[:4000], "from": from_,
                            "date": date_iso, "thread_id": thread_id,
                            "gmail_id": msg_id, "label_ids": label_ids,
                            "has_attachments": bool(attachments),
                            "attachments": attachments,
                            "projects": projs,
                        },
                    )]
                )
                stats["qdrant_ok"] += 1
            except Exception as e:
                print(f"  ⚠ Qdrant [{subject[:40]}]: {e}")

            done_ids.add(msg_id)
            processed += 1
            stats["total"] += 1

            if processed % SAVE_EVERY == 0:
                progress.update({"done_ids": list(done_ids),
                                 "page_token": page_token, "stats": stats})
                save_progress(progress)
                print(f"  [{processed}] retain:{stats['retain_ok']} "
                      f"qdrant:{stats['qdrant_ok']} fail:{stats['retain_fail']}")

            if args.max and processed >= args.max:
                break

        if not page_token or (args.max and processed >= args.max):
            break

    progress.update({"done_ids": list(done_ids), "page_token": None, "stats": stats})
    save_progress(progress)
    print(f"\n完成！{json.dumps(stats, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
