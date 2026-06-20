#!/usr/bin/env python3
"""
email-to-hindsight: process_thread.py
將 Gmail thread 解析、清理並寫入 Hindsight。

使用方式：
  python process_thread.py --input-json thread.json
  python process_thread.py --input-json thread.json --dry-run
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

# ── 設定 ────────────────────────────────────────────────────────────────────────

HINDSIGHT_BASE   = "http://hindsight:8888"
HINDSIGHT_BANK   = "shuhsing"

OLLAMA_BASE      = "http://ollama:11434"
OLLAMA_MODEL     = "gemma4:e4b"

MAX_THREAD_BYTES = 50_000

IGNORE_ATTACHMENT = re.compile(r'^(ecblank\.gif|graycol\.gif|0\d{7}\.gif)$')
DOMINO_QUOTE      = re.compile(r'[一-鿿\w]{2,10}---\d{4}/\d{2}/\d{2}')
REPLY_PREFIX      = re.compile(r'^(回覆:|Re:|RE:|FW:|fw:)\s*', re.IGNORECASE)
TZ_TAIPEI         = timezone(timedelta(hours=8))

# ── HTTP 工具 ────────────────────────────────────────────────────────────────────

def http_post(url: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())

# ── Ollama LLM ───────────────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """你是永光化學的信件分類助理。根據以下信件討論串，產出結構化 JSON。

分類規則：
- category：採購議價、技術討論、IT系統、行政通知、專案協調、其他
- project：如無明確專案名稱填空字串
- participants 必須包含所有出現過的人，role 從：詢價方、廠商業務、IT支援、主管、同仁 選擇
- archive_path 格式：{{category}}/{{year}}/（year 取第一封信的年份）
- tags 使用格式：topic:xxx、vendor:xxx、project:xxx、dept:xxx
- hindsight_context：繁體中文，60字以內，說明這串信件的核心主題與結果

信件摘要：
{summary}

只回傳 JSON，不要 markdown 包裹，不要其他文字。格式：
{{
  "category": "",
  "project": "",
  "participants": [
    {{"name": "", "org": "", "role": ""}}
  ],
  "archive_path": "",
  "tags": [],
  "hindsight_context": ""
}}"""


def classify_thread(emails: list) -> dict:
    parts = []
    for e in emails[:5]:
        parts.append(
            f"[{e['index']}] {e['timestamp'][:10]} "
            f"{e['from']} → {', '.join(e['to'])}\n"
            f"{e['body'][:300]}"
        )
    summary = "\n---\n".join(parts)
    prompt  = CLASSIFY_PROMPT.format(summary=summary)

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0}
    }
    resp = http_post(f"{OLLAMA_BASE}/api/chat", payload)
    raw  = resp["message"]["content"].strip()
    raw  = re.sub(r'^```json\s*', '', raw)
    raw  = re.sub(r'\s*```$',     '', raw)
    return json.loads(raw)

# ── HTML 清理 ────────────────────────────────────────────────────────────────────

def extract_clean_body(html_body: str) -> str:
    if not html_body:
        return ""
    soup = BeautifulSoup(html_body, 'html.parser')
    for img in soup.find_all('img'):
        img.decompose()
    text  = soup.get_text(separator='\n')
    lines = text.split('\n')
    clean = []
    for line in lines:
        s = line.strip()
        if DOMINO_QUOTE.search(s):
            break
        if re.match(r'^(寄件人|From|收件者|To|副本抄送|CC|日期|Date|主旨|Subject)[\s:：]', s):
            break
        clean.append(line)
    result = '\n'.join(clean).strip()
    return re.sub(r'\n{3,}', '\n\n', result)


def filter_attachments(attachments: list) -> list:
    return [
        {"name": a["filename"], "mimeType": a["mimeType"]}
        for a in attachments
        if not IGNORE_ATTACHMENT.match(a.get("filename", ""))
    ]

# ── subject 工具 ─────────────────────────────────────────────────────────────────

def normalize_subject(subject: str) -> str:
    return REPLY_PREFIX.sub('', subject).strip()


def make_document_id(subject: str) -> str:
    clean = normalize_subject(subject)
    slug  = re.sub(r'[^一-鿿A-Za-z0-9]+', '-', clean)
    return f"email-thread-{slug.strip('-')[:60]}"

# ── 解析 Gmail message ───────────────────────────────────────────────────────────

def parse_message(msg: dict, index: int) -> dict:
    body = extract_clean_body(msg.get("htmlBody", ""))

    if not body and msg.get("plaintextBody"):
        lines = msg["plaintextBody"].split('\n')
        clean = []
        for line in lines:
            if DOMINO_QUOTE.search(line.strip()):
                break
            clean.append(line)
        body = '\n'.join(clean).strip()

    raw_date  = msg.get("date", "")
    timestamp = raw_date if isinstance(raw_date, str) and raw_date \
                else datetime.now(TZ_TAIPEI).isoformat()

    return {
        "index":      index,
        "message_id": msg.get("id", ""),
        "timestamp":  timestamp,
        "from":       msg.get("sender", "").replace("%local", ""),
        "to":         msg.get("toRecipients", []),
        "cc":         msg.get("ccRecipients", []),
        "attachments": filter_attachments(msg.get("attachments", [])),
        "body":       body,
    }


def merge_threads(thread_list: list) -> dict:
    seen, all_msgs, subject, ids = set(), [], "", []
    for t in thread_list:
        ids.append(t.get("id", ""))
        if not subject:
            subject = (t.get("messages") or [{}])[0].get("subject", "")
        for msg in t.get("messages", []):
            if msg.get("id") not in seen:
                seen.add(msg["id"])
                all_msgs.append(msg)
    all_msgs.sort(key=lambda m: m.get("date", ""))
    return {"thread_ids": ids, "subject": normalize_subject(subject), "messages": all_msgs}

# ── Hindsight retain ─────────────────────────────────────────────────────────────

def retain(item: dict):
    payload = {"async": True, "items": [item]}
    result  = http_post(f"{HINDSIGHT_BASE}/v1/default/banks/{HINDSIGHT_BANK}/memories", payload)
    print(f"  ✅ retain ok — {result.get('items_count', '?')} item(s)")


def retain_thread(thread: dict, emails: list, classification: dict, dry_run: bool):
    content = {
        "thread_ids":   thread["thread_ids"],
        "subject":      thread["subject"],
        "category":     classification.get("category", ""),
        "emails":       emails,
        "participants": classification.get("participants", []),
    }
    item = {
        "content":     json.dumps(content, ensure_ascii=False),
        "document_id": make_document_id(thread["subject"]),
        "context":     classification.get("hindsight_context", thread["subject"]),
        "timestamp":   emails[0]["timestamp"],
        "tags":        classification.get("tags", []),
        "metadata": {
            "source":       "gmail",
            "thread_ids":   str(thread["thread_ids"]),
            "subject":      thread["subject"],
            "archive_path": classification.get("archive_path", ""),
            "email_count":  str(len(emails)),
        },
    }

    size = len(item["content"].encode("utf-8"))

    if dry_run:
        print(f"\n[DRY RUN]")
        print(f"  document_id : {item['document_id']}")
        print(f"  context     : {item['context']}")
        print(f"  timestamp   : {item['timestamp']}")
        print(f"  tags        : {item['tags']}")
        print(f"  content     : {size/1024:.1f} KB")
        return

    if size > MAX_THREAD_BYTES:
        print(f"⚠️  {size/1024:.1f}KB > 50KB，逐封 retain")
        base = item["document_id"]
        for i, email in enumerate(emails):
            single = {"subject": thread["subject"], "emails": [email]}
            retain({
                "content":     json.dumps(single, ensure_ascii=False),
                "document_id": f"{base}-msg-{i+1}",
                "context":     f"{item['context']}（第{i+1}封）",
                "timestamp":   email["timestamp"],
                "tags":        item["tags"],
                "metadata":    item["metadata"],
            })
    else:
        retain(item)

# ── 主流程 ───────────────────────────────────────────────────────────────────────

def process(thread_list: list, dry_run: bool = False):
    thread = merge_threads(thread_list)
    print(f"主旨：{thread['subject']}")
    print(f"Thread IDs：{', '.join(thread['thread_ids'])}")
    print(f"Messages：{len(thread['messages'])} 封\n")

    emails = [parse_message(msg, i+1) for i, msg in enumerate(thread["messages"])]
    for e in emails:
        print(f"  [{e['index']}] {e['timestamp'][:16]}  {e['from'][:35]}"
              f"  body={len(e['body'])}字  att={len(e['attachments'])}")

    print("\n🤖 LLM 分類中...")
    classification = classify_thread(emails)
    print(f"  category : {classification.get('category')}")
    print(f"  context  : {classification.get('hindsight_context')}")
    print(f"  tags     : {classification.get('tags')}")

    print("\n💾 寫入 Hindsight...")
    retain_thread(thread, emails, classification, dry_run)

# ── CLI ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Email → Hindsight")
    parser.add_argument("--input-json", required=True,
                        help="Gmail thread JSON 檔（list 或單一 object）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只顯示結果，不寫入 Hindsight")
    parser.add_argument("--bank", default=None,
                        help="Hindsight bank ID（覆蓋預設值）")
    args = parser.parse_args()

    if args.bank:
        HINDSIGHT_BANK = args.bank  # noqa: F841 — intentional global override

    with open(args.input_json) as f:
        data = json.load(f)
    thread_list = data if isinstance(data, list) else [data]

    process(thread_list, dry_run=args.dry_run)
