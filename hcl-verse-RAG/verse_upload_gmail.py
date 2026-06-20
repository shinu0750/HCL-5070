#!/usr/bin/env python3
"""
Verse EML → Gmail 上傳（參數化版，供 hcl-verse-RAG pipeline 串接）
====================================================================
把指定資料夾的 .eml 批次 import 到 Gmail，貼標籤，成功後搬到 done 資料夾。
沿用既有 OAuth 憑證/token、fix_eml_content、log 去重邏輯。

用法：
    python3 verse_upload_gmail.py [eml_folder] [--label L] [--done DIR] [--log FILE]

預設：
    eml_folder = ~/verse-export
    --label    = Notes_Import
    --done     = ~/Documents/eml to gamil/eml_done
    憑證/token = ~/Documents/eml to gamil/{credentials,token}.json
"""
import os, tempfile, sys, base64, time, json, shutil, re, argparse
from pathlib import Path
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── 固定路徑（沿用既有 OAuth 設定）────────────────────────────────────────────
GMAIL_DIR        = "/Users/shuhsing/Documents/eml to gamil"
CREDENTIALS_FILE = os.path.join(GMAIL_DIR, "credentials.json")
TOKEN_FILE       = os.path.join(GMAIL_DIR, "token.json")
SCOPES           = ['https://www.googleapis.com/auth/gmail.modify']
OUTPUT_FILE      = os.path.join(tempfile.gettempdir(), "verse_upload_gmail_result.json")
BATCH_SIZE       = 50
DELAY_SECONDS    = 1


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("eml_folder", nargs="?", default=os.path.expanduser("~/verse-export"))
    ap.add_argument("--label", default="Notes_Import")
    ap.add_argument("--done",  default=os.path.join(GMAIL_DIR, "eml_done"))
    ap.add_argument("--log",   default=os.path.join(GMAIL_DIR, "verse_upload_log.txt"))
    return ap.parse_args()


def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"❌ 找不到 {CREDENTIALS_FILE}")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return creds


def get_or_create_label(service, label_name):
    if not label_name:
        return None
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for label in labels:
        if label['name'] == label_name:
            return label['id']
    created = service.users().labels().create(userId='me', body={
        'name': label_name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'
    }).execute()
    return created['id']


def fix_eml_content(raw_content):
    """修復 .eml 的 From 欄位（缺則補、重複則只留第一個）。"""
    try:
        text = raw_content.decode('utf-8', errors='replace')
    except Exception:
        text = raw_content.decode('latin-1', errors='replace')
    from_lines = re.findall(r'^From:.*$', text, re.MULTILINE | re.IGNORECASE)
    if len(from_lines) == 0:
        text = "From: shuhsing@ecic.com.tw\r\n" + text
    elif len(from_lines) > 1:
        first = [True]
        def replace_from(m):
            if first[0]:
                first[0] = False
                return m.group(0)
            return ''
        text = re.sub(r'^From:.*$', replace_from, text, flags=re.MULTILINE | re.IGNORECASE)
    return text.encode('utf-8', errors='replace')


def upload_eml(service, eml_path, label_id=None):
    try:
        with open(eml_path, 'rb') as f:
            raw_content = fix_eml_content(f.read())
        raw_encoded = base64.urlsafe_b64encode(raw_content).decode('utf-8')
        body = {'raw': raw_encoded}
        if label_id:
            body['labelIds'] = [label_id, 'INBOX']
        result = service.users().messages().import_(
            userId='me', body=body, neverMarkSpam=True, processForCalendar=False,
            internalDateSource='dateHeader'
        ).execute()
        return True, result.get('id', 'unknown')
    except HttpError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def load_progress(log_file):
    uploaded = set()
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('SUCCESS:'):
                    uploaded.add(line.replace('SUCCESS:', '').strip())
    return uploaded


def save_log(log_file, status, filename, detail=''):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a', encoding='utf-8') as f:
        if status == 'SUCCESS':
            f.write(f"SUCCESS: {filename}\n")
        else:
            f.write(f"FAILED: {filename} | {detail} | {ts}\n")


def main():
    args = parse_args()
    eml_dir = Path(args.eml_folder)
    if not eml_dir.exists():
        print(f"❌ 找不到資料夾：{args.eml_folder}")
        json.dump({"error": "eml_folder_not_found", "eml_folder": str(eml_dir)},
                  open(OUTPUT_FILE, "w"), ensure_ascii=False)
        sys.exit(1)

    all_eml = sorted(f for f in eml_dir.rglob("*") if f.suffix.lower().strip() == '.eml')
    remaining = all_eml

    print(f"📧 {args.eml_folder} 共 {len(all_eml)} 封，待上傳 {len(remaining)}")
    if not remaining:
        print("🎉 沒有待上傳的 EML")
        json.dump({"total": len(all_eml), "uploaded": 0, "failed": 0,
                   "label": args.label, "done_folder": args.done, "results": []},
                  open(OUTPUT_FILE, "w"), ensure_ascii=False, indent=2)
        return

    creds = authenticate()
    service = build('gmail', 'v1', credentials=creds)
    label_id = get_or_create_label(service, args.label)
    print(f"🏷  標籤：{args.label}\n🚀 開始上傳...")

    done_path = Path(args.done)
    done_path.mkdir(parents=True, exist_ok=True)
    success, fail, results = 0, 0, []

    for i, eml_file in enumerate(remaining, 1):
        ok, detail = upload_eml(service, eml_file, label_id)
        if ok:
            success += 1
            save_log(args.log, 'SUCCESS', str(eml_file))
            dest = done_path / eml_file.name
            if dest.exists():
                n = 2
                while (done_path / f"{eml_file.stem}_{n}{eml_file.suffix}").exists():
                    n += 1
                dest = done_path / f"{eml_file.stem}_{n}{eml_file.suffix}"
            shutil.move(str(eml_file), str(dest))
            results.append({"file": eml_file.name, "status": "uploaded", "gmail_id": detail})
            print(f"✅ [{i}/{len(remaining)}] {eml_file.name[:50]}")
        else:
            fail += 1
            save_log(args.log, 'FAILED', str(eml_file), detail)
            results.append({"file": eml_file.name, "status": "failed", "detail": detail[:200]})
            print(f"❌ [{i}/{len(remaining)}] {eml_file.name[:50]} — {detail[:80]}")
        if i % BATCH_SIZE == 0:
            time.sleep(DELAY_SECONDS)

    summary = {"total": len(all_eml), "uploaded": success, "failed": fail,
               "label": args.label, "done_folder": str(done_path), "results": results}
    json.dump(summary, open(OUTPUT_FILE, "w"), ensure_ascii=False, indent=2)
    print(f"\n✓ 上傳完成：成功 {success}、失敗 {fail} → 標籤「{args.label}」，已搬到 {done_path}")
    print(f"  結果已寫入 {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
