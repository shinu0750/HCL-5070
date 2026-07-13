#!/usr/bin/env python3
"""
Verse EML → Gmail 上傳（參數化版，供 hcl-verse-RAG pipeline 串接）
====================================================================
把指定資料夾的 .eml 批次 import 到 Gmail，貼標籤，成功後搬到 done 資料夾。
沿用既有 OAuth 憑證/token、fix_eml_content、log 去重邏輯。

用法：
    python3 verse_upload_gmail.py [eml_folder] [--label L] [--done DIR] [--log FILE]

預設（EML_OUTPUT_DIR 環境變數可覆寫共用網路磁碟根目錄，預設
//10.11.1.40/工程管理暨智慧製造處/公用區-Hermes/eml）：
    eml_folder = {EML_OUTPUT_DIR}/Undo   （待上傳，不分帳號各自存）
    --label    = Notes_Import_v2
    --done     = {EML_OUTPUT_DIR}/Done   （已上傳，同樣是共用網路磁碟）
    憑證/token = ~/Documents/eml to gamil/{credentials,token}.json
    （GMAIL_OAUTH_DIR 環境變數可覆寫成其他帳號專用的憑證/token 目錄，
    但 Undo/Done 一律是共用網路磁碟，不會因為換帳號而分開存）
"""
import os, sys, base64, time, json, shutil, re, argparse, tempfile
from pathlib import Path
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Windows 主控台預設用 cp950（Big5），印不出 ✓/✗/📧 等符號會直接 UnicodeEncodeError 崩潰
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── 固定路徑（沿用既有 OAuth 設定）────────────────────────────────────────────
# 代簽別人帳號測試時，用 GMAIL_OAUTH_DIR 指向另一組 credentials.json/token.json，
# 避免覆蓋自己的 token（仿照 hcl-notes-approval 的 HCL_ENV_FILE 多帳號模式）
GMAIL_DIR        = os.environ.get("GMAIL_OAUTH_DIR", os.path.expanduser("~/Documents/eml to gamil"))
CREDENTIALS_FILE = os.path.join(GMAIL_DIR, "credentials.json")
TOKEN_FILE       = os.path.join(GMAIL_DIR, "token.json")
SCOPES           = ['https://www.googleapis.com/auth/gmail.modify']
OUTPUT_FILE      = os.path.join(tempfile.gettempdir(), "verse_upload_gmail_result.json")
BATCH_SIZE       = 50
DELAY_SECONDS    = 1


# EML 分支 B 實際存放位置：部門共用網路磁碟（跟 verse_archive_pipeline.py 的
# EML_OUTPUT_DIR 用同一組預設值 + 同名環境變數，維持兩邊一致，不用互相 import）。
# 待上傳／已上傳都放在同一個共用資料夾底下的 Undo/Done 子目錄，不分帳號各自存
# 本機——之前用「各帳號本機 eml_done」設計時，共用 Undo 池裡誰的信件都混在一起，
# 用不同帳號的 GMAIL_OAUTH_DIR 跑上傳會把整個共用池的信全部掃進當次那個帳號的
# Gmail（實測發生過），改成 Done 也放共用網路磁碟後，至少能讓所有人看到目前
# Undo 裡還累積了哪些待上傳的信、Done 裡已經上傳過哪些，不會被鎖在某人的本機
# Documents 資料夾看不到
EML_ROOT_DIR = os.environ.get(
    "EML_OUTPUT_DIR", r"\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\eml")
DEFAULT_EML_DIR = os.path.join(EML_ROOT_DIR, "Undo")
DEFAULT_DONE_DIR = os.path.join(EML_ROOT_DIR, "Done")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("eml_folder", nargs="?", default=DEFAULT_EML_DIR)
    ap.add_argument("--label", default="Notes_Import_v2")
    ap.add_argument("--done",  default=DEFAULT_DONE_DIR)
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
                  open(OUTPUT_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        sys.exit(1)

    all_eml = sorted(f for f in eml_dir.rglob("*") if f.suffix.lower().strip() == '.eml')
    # load_progress() 讀 log 裡記錄過的 SUCCESS 路徑，排除掉已經上傳成功的檔案——
    # 正常情況下上傳成功會把檔案搬出 eml_dir，下次自然掃不到；這層過濾要防的是
    # 「上傳成功但搬移那一步失敗」的邊界情況：檔案還留在原地，若沒有這層過濾，
    # 重跑會把同一封信再匯入 Gmail 一次，造成重複
    already_uploaded = load_progress(args.log)
    remaining = [f for f in all_eml if str(f) not in already_uploaded]
    skipped_already = len(all_eml) - len(remaining)

    print(f"📧 {args.eml_folder} 共 {len(all_eml)} 封，待上傳 {len(remaining)}"
          + (f"（略過 {skipped_already} 封 log 記錄已上傳過）" if skipped_already else ""))
    if not remaining:
        print("🎉 沒有待上傳的 EML")
        json.dump({"total": len(all_eml), "skipped_already_uploaded": skipped_already,
                   "uploaded": 0, "failed": 0,
                   "label": args.label, "done_folder": args.done, "results": []},
                  open(OUTPUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
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

    summary = {"total": len(all_eml), "skipped_already_uploaded": skipped_already,
               "uploaded": success, "failed": fail,
               "label": args.label, "done_folder": str(done_path), "results": results}
    json.dump(summary, open(OUTPUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n✓ 上傳完成：成功 {success}、失敗 {fail} → 標籤「{args.label}」，已搬到 {done_path}")
    print(f"  結果已寫入 {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
