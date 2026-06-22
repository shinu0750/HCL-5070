import json, re
from bs4 import BeautifulSoup

DOMINO_QUOTE = re.compile(r'[一-鿿\w]{2,10}---\d{4}/\d{2}/\d{2}')

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

with open("/scripts/email-to-hindsight/test_googlechat_thread.json") as f:
    threads = json.load(f)

seen, all_msgs = set(), []
for t in threads:
    for msg in t.get("messages", []):
        if msg["id"] not in seen:
            seen.add(msg["id"])
            all_msgs.append(msg)
all_msgs.sort(key=lambda m: m.get("date", ""))

print(f"共 {len(all_msgs)} 封信\n")
for i, msg in enumerate(all_msgs, 1):
    body = clean_body(msg)
    print(f"[{i}] {msg['date'][:16]}  {msg['sender'][:40]}")
    print(f"     body({len(body)}字): {repr(body[:150])}")
    print()
