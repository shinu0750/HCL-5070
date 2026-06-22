#!/usr/bin/env python3
"""
OneByOne 模式：每個 thread 各自獨立寫入 Hindsight。
document_id = email-thread-{thread_id}
同一 thread 內多封信合併為一筆，不同 thread 各自獨立。

用法：
  python process_one_by_one.py --input-json input_threads.json
  python process_one_by_one.py --input-json input_threads.json --dry-run
"""

import argparse, json, re, urllib.request
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

HINDSIGHT_BASE = "http://hindsight:8888"
HINDSIGHT_BANK = "shuhsing"
OLLAMA_BASE    = "http://ollama:11434"
OLLAMA_MODEL   = "gemma4:e4b"

DOMINO_QUOTE = re.compile(r'[一-鿿\w]{2,10}---\d{4}/\d{2}/\d{2}')
REPLY_PREFIX = re.compile(r'^(回覆:|Re:|RE:|FW:|fw:)\s*', re.IGNORECASE)
IGNORE_ATTACHMENT = re.compile(r'^(ecblank\.gif|graycol\.gif|notesdoclink\.gif|doclink\.gif|0\d{7}\.gif)$')
TZ_TAIPEI = timezone(timedelta(hours=8))

CLASSIFY_PROMPT = """你是永光化學的信件分類助理。根據以下信件討論串，產出結構化 JSON。

分類規則：
- category：採購議價、技術討論、IT系統、行政通知、專案協調、其他
- project：如無明確專案名稱填空字串
- role 從：詢價方、廠商業務、IT支援、主管、同仁 選擇
- tags 使用格式：topic:xxx、vendor:xxx、project:xxx、dept:xxx
- hindsight_context：繁體中文，60字以內，說明這個討論串的核心內容

信件內容：
{content}

只回傳 JSON，不要 markdown 包裹，不要其他文字。格式：
{{
  "category": "",
  "project": "",
  "participants": [
    {{"name": "", "org": "", "role": ""}}
  ],
  "tags": [],
  "hindsight_context": ""
}}"""


def http_post(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def clean_body(msg):
    html  = msg.get("htmlBody") or ""
    plain = msg.get("plaintextBody") or ""
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            img.decompose()
        text = soup.get_text(separator="\n")
    else:
        text = plain
    lines, result = text.split("\n"), []
    for line in lines:
        s = line.strip()
        if DOMINO_QUOTE.search(s):
            break
        if re.match(r'^(寄件人|From|收件者|To|副本抄送|CC|日期|Date|主旨|Subject)[\s:：]', s):
            break
        result.append(line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(result).strip())


def filter_attachments(attachments):
    return [
        {"name": a["filename"], "mimeType": a["mimeType"]}
        for a in attachments
        if not IGNORE_ATTACHMENT.match(a.get("filename", ""))
    ]


def classify_thread(combined_body, subject):
    snippet = f"主旨：{subject}\n\n{combined_body[:800]}"
    prompt  = CLASSIFY_PROMPT.format(content=snippet)
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0}
    }
    resp = http_post(f"{OLLAMA_BASE}/api/chat", payload)
    raw  = resp["message"]["content"].strip()
    raw  = re.sub(r'^```json\s*', '', raw)
    raw  = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)


def retain_item(item):
    payload = {"async": True, "items": [item]}
    result  = http_post(
        f"{HINDSIGHT_BASE}/v1/default/banks/{HINDSIGHT_BANK}/memories", payload
    )
    return result.get("items_count", "?")


def process(thread_list, dry_run=False):
    print(f"共 {len(thread_list)} 個 thread\n")

    for i, thread in enumerate(thread_list, 1):
        thread_id = thread.get("id", "")
        doc_id    = f"email-thread-{thread_id}"
        msgs      = thread.get("messages", [])
        if not msgs:
            continue

        # 基本資訊取第一封
        subject = REPLY_PREFIX.sub('', msgs[0].get("subject", "")).strip()
        ts_first = msgs[0].get("date", datetime.now(TZ_TAIPEI).isoformat())
        ts_last  = msgs[-1].get("date", ts_first)

        # 合併所有信的 body
        message_bodies = []
        all_atts = []
        all_participants = []
        senders = []
        for msg in msgs:
            body = clean_body(msg)
            sender = msg.get("sender", "").replace("%local", "")
            senders.append(sender)
            ts = msg.get("date", "")[:16]
            message_bodies.append(f"[{ts}] {sender}\n{body}")
            all_atts.extend(filter_attachments(msg.get("attachments", [])))

        combined_body = "\n\n---\n\n".join(message_bodies)

        print(f"[{i}/{len(thread_list)}] thread: {thread_id}")
        print(f"       subject : {subject}")
        print(f"       doc_id  : {doc_id}")
        print(f"       信件數  : {len(msgs)} 封  ({ts_first[:10]} ~ {ts_last[:10]})")
        print(f"       body    : {len(combined_body)}字  att={len(all_atts)}")

        # LLM 分類（以整串為單位）
        print(f"       🤖 分類中...", end="", flush=True)
        try:
            cls = classify_thread(combined_body, subject)
            print(f" {cls.get('category')} / {cls.get('hindsight_context', '')[:40]}")
        except Exception as e:
            print(f" ❌ 分類失敗：{e}")
            cls = {"category": "其他", "tags": [], "hindsight_context": subject, "participants": []}

        content = {
            "thread_id":    thread_id,
            "subject":      subject,
            "category":     cls.get("category", ""),
            "date_first":   ts_first,
            "date_last":    ts_last,
            "email_count":  len(msgs),
            "senders":      list(dict.fromkeys(senders)),
            "attachments":  all_atts,
            "messages":     [
                {
                    "message_id": m.get("id", ""),
                    "date":       m.get("date", ""),
                    "from":       m.get("sender", "").replace("%local", ""),
                    "to":         m.get("toRecipients", []),
                    "cc":         m.get("ccRecipients", []),
                    "body":       clean_body(m),
                }
                for m in msgs
            ],
            "participants": cls.get("participants", []),
        }

        item = {
            "content":     json.dumps(content, ensure_ascii=False),
            "document_id": doc_id,
            "context":     cls.get("hindsight_context", subject),
            "timestamp":   ts_first,
            "tags":        cls.get("tags", []),
            "metadata": {
                "source":       "gmail",
                "thread_id":    thread_id,
                "subject":      subject,
                "category":     cls.get("category", ""),
                "email_count":  str(len(msgs)),
            },
        }

        if dry_run:
            print(f"       [DRY RUN] content={len(item['content'].encode())//1024}KB  tags={item['tags']}\n")
        else:
            count = retain_item(item)
            print(f"       ✅ 寫入完成 ({count} item)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Email → Hindsight (OneByOne by thread)")
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bank", default=None,
                        help="Hindsight bank ID（覆蓋預設值）")
    args = parser.parse_args()

    if args.bank:
        HINDSIGHT_BANK = args.bank  # noqa: F841

    with open(args.input_json) as f:
        data = json.load(f)
    thread_list = data if isinstance(data, list) else [data]
    process(thread_list, dry_run=args.dry_run)
