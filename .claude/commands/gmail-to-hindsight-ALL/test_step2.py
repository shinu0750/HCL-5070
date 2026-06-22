import json, re, urllib.request
from bs4 import BeautifulSoup

DOMINO_QUOTE = re.compile(r'[一-鿿\w]{2,10}---\d{4}/\d{2}/\d{2}')
OLLAMA_BASE  = "http://ollama:11434"
OLLAMA_MODEL = "gemma4:e4b"

def clean_body(msg):
    html  = msg.get("htmlBody")  or ""
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

def classify(emails):
    parts = []
    for e in emails[:5]:
        parts.append(
            f"[{e['index']}] {e['timestamp'][:10]} {e['from']} → {', '.join(e['to'])}\n"
            f"{e['body'][:300]}"
        )
    summary = "\n---\n".join(parts)
    prompt  = CLASSIFY_PROMPT.format(summary=summary)

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0}
    }, ensure_ascii=False).encode("utf-8")

    req  = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())

    raw = result["message"]["content"].strip()
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)

# ── 讀取並解析 ──
with open("/scripts/email-to-hindsight/test_googlechat_thread.json") as f:
    threads = json.load(f)

seen, all_msgs = set(), []
for t in threads:
    for msg in t.get("messages", []):
        if msg["id"] not in seen:
            seen.add(msg["id"])
            all_msgs.append(msg)
all_msgs.sort(key=lambda m: m.get("date", ""))

emails = []
for i, msg in enumerate(all_msgs, 1):
    emails.append({
        "index":     i,
        "timestamp": msg.get("date", ""),
        "from":      msg.get("sender", "").replace("%local", ""),
        "to":        msg.get("toRecipients", []),
        "body":      clean_body(msg),
    })

print("🤖 呼叫 Ollama 分類中...")
result = classify(emails)
print("\n=== 分類結果 ===")
print(json.dumps(result, ensure_ascii=False, indent=2))
