#!/usr/bin/env python3
"""
fetch_gmail_month.py — Gmail 特定月份信件 → JSON

輸出格式與 process_one_by_one.py 相容（同 Verse 版共用格式）

用法：
    python fetch_gmail_month.py --year 2026 --month 6
    python fetch_gmail_month.py --year 2026 --month 6 --output C:/tmp/gmail_2026_06.json
    python fetch_gmail_month.py --year 2026 --month 6 --dry-run
    python fetch_gmail_month.py --year 2026 --month 6 --limit 20
"""
import os, sys, json, base64, argparse, re, calendar
from pathlib import Path
from email.utils import parsedate_to_datetime
from email.header import decode_header as _decode_header

CREDENTIALS_FILE   = Path(r"C:\Users\EID\Documents\Claude\ShuHsing\credentials.json")
TOKEN_FILE         = Path(r"C:\Users\EID\Documents\Claude\ShuHsing\token.json")
INTERNAL_CONTACTS  = Path(r"C:\Users\EID\Documents\Claude\ShuHsing\ecic_contacts.json")
EXTERNAL_CONTACTS  = Path(__file__).parent / "contacts_external.json"
SCOPES             = ["https://www.googleapis.com/auth/gmail.readonly"]


def load_email_lookup():
    """回傳 {email_lower: 名字} 對照表，內部優先"""
    lookup = {}
    # 外部名片
    if EXTERNAL_CONTACTS.exists():
        with open(EXTERNAL_CONTACTS, encoding="utf-8-sig") as f:
            for email, name in json.load(f).items():
                lookup[email.lower()] = name
    # 內部員工（覆蓋外部同 email）
    if INTERNAL_CONTACTS.exists():
        with open(INTERNAL_CONTACTS, encoding="utf-8") as f:
            data = json.load(f)
        for name, email in data["contacts"].items():
            lookup[email.lower()] = name
    return lookup


def authenticate():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        print(f"token 已儲存：{TOKEN_FILE}")
    return creds


def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def decode_mime_header(value):
    """RFC 2047 encoded header (e.g. =?big5?B?...?=) → unicode string"""
    if not value:
        return ""
    parts = _decode_header(value)
    result = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


def resolve_name(email, display_name, lookup, unmatched):
    """email → 名字，找不到時記錄到 unmatched 並回傳 display_name 或 email"""
    key = email.lower()
    if key in lookup:
        return lookup[key]
    unmatched.add(email)
    return display_name or email


def parse_addresses(raw, lookup, unmatched):
    """'Name <email>, Name2 <email2>' → [名字或 email, ...]"""
    if not raw:
        return []
    results = []
    for part in raw.split(","):
        part = part.strip()
        m = re.search(r'<(.+?)>', part)
        if m:
            email = m.group(1)
            display = re.sub(r'\s*<.+?>\s*', '', part).strip().strip('"')
            results.append(resolve_name(email, display, lookup, unmatched))
        elif part:
            results.append(resolve_name(part, part, lookup, unmatched))
    return [r for r in results if r]


def extract_sender_name(raw, lookup, unmatched):
    if not raw:
        return ""
    m = re.search(r'<(.+?)>', raw)
    if m:
        email = m.group(1)
        display = re.sub(r'\s*<.+?>\s*', '', raw).strip().strip('"')
        return resolve_name(email, display, lookup, unmatched)
    return resolve_name(raw.strip(), raw.strip(), lookup, unmatched)


def get_charset(payload):
    """從 Content-Type header 取 charset，找不到回傳 None"""
    for h in payload.get("headers", []):
        if h["name"].lower() == "content-type":
            m = re.search(r'charset=["\']?([^"\';\s]+)', h["value"], re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


def decode_bytes(raw, charset):
    """charset_normalizer 優先偵測實際編碼，宣告的 charset 只做 fallback"""
    from charset_normalizer import from_bytes
    result = from_bytes(raw).best()
    if result:
        return str(result)
    if charset:
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


def extract_plain(payload):
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data + "==")
            return decode_bytes(raw, get_charset(payload))
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = extract_plain(part)
            if text:
                return text
    return ""


def extract_attachments(payload):
    results = []
    filename = payload.get("filename", "")
    if filename:
        headers_dict = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        disposition = headers_dict.get("content-disposition", "")
        if disposition.lower().startswith("attachment"):
            results.append({
                "filename": filename,
                "mimeType": payload.get("mimeType", ""),
            })
    for part in payload.get("parts", []):
        results.extend(extract_attachments(part))
    return results


def normalize_date(date_raw):
    try:
        return parsedate_to_datetime(date_raw).isoformat()
    except Exception:
        return date_raw or ""


def list_threads(service, year, month):
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    q = f"after:{year}/{month:02d}/01 before:{next_year}/{next_month:02d}/01"
    print(f"查詢：{q}")

    threads, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": q, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().threads().list(**kwargs).execute()
        threads.extend(result.get("threads", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return threads


def fetch_month(service, year, month, limit=0, dry_run=False):
    lookup    = load_email_lookup()
    unmatched = set()

    threads = list_threads(service, year, month)
    print(f"找到 {len(threads)} 個 thread")

    if limit:
        threads = threads[:limit]
        print(f"限制處理前 {limit} 個")

    output = []
    for i, t in enumerate(threads):
        thread_id = t["id"]

        if dry_run:
            print(f"  [{i+1}/{len(threads)}] thread {thread_id}")
            continue

        thread_data = service.users().threads().get(
            userId="shuhsing@ecic.com.tw", id=thread_id, format="full"
        ).execute()

        messages_out = []
        for msg in thread_data.get("messages", []):
            payload  = msg.get("payload", {})
            headers  = payload.get("headers", [])
            subject  = decode_mime_header(get_header(headers, "Subject")) or "(無主旨)"
            date_raw = get_header(headers, "Date") or ""
            body     = extract_plain(payload).strip() or msg.get("snippet", "")

            messages_out.append({
                "id":            msg["id"],
                "date":          normalize_date(date_raw),
                "subject":       subject,
                "sender":        extract_sender_name(get_header(headers, "From"), lookup, unmatched),
                "toRecipients":  parse_addresses(get_header(headers, "To"), lookup, unmatched),
                "ccRecipients":  parse_addresses(get_header(headers, "Cc"), lookup, unmatched),
                "attachments":   extract_attachments(payload),
                "plaintextBody": body,
            })

        output.append({"id": thread_id, "messages": messages_out})

        if (i + 1) % 20 == 0 or (i + 1) == len(threads):
            print(f"  [{i+1}/{len(threads)}] 已處理")

    if unmatched:
        print(f"\n⚠ 未對照到名字的 email（{len(unmatched)} 筆）：")
        for e in sorted(unmatched):
            print(f"  {e}")

    return output


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year",    type=int, required=True,  help="年份，例如 2026")
    ap.add_argument("--month",   type=int, required=True,  help="月份，例如 6")
    ap.add_argument("--output",  default="",               help="輸出 JSON 路徑（預設自動命名到 Temp）")
    ap.add_argument("--limit",   type=int, default=0,      help="只處理前 N 個 thread（0=全部）")
    ap.add_argument("--dry-run", action="store_true",      help="只列出 thread ID，不讀內文")
    args = ap.parse_args()

    from googleapiclient.discovery import build
    creds   = authenticate()
    service = build("gmail", "v1", credentials=creds)

    data = fetch_month(service, args.year, args.month, args.limit, args.dry_run)

    if args.dry_run:
        print("dry-run 完成，未寫入檔案")
        return

    output_path = args.output or rf"C:\Users\EID\AppData\Local\Temp\gmail_{args.year}_{args.month:02d}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n完成：{len(data)} 個 thread → {output_path}")


if __name__ == "__main__":
    main()
