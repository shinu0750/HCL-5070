#!/usr/bin/env python3
"""
本機/網路資料夾 .eml 歸檔 pipeline（hcl-verse-RAG 的變體）
==========================================================
跟 verse_archive_pipeline.py 邏輯一致（RAG/Hindsight + EML/Gmail 兩分支），
差別只在信件來源：不是即時爬蟲 Verse 的「04Done」資料夾，而是一批已經匯出好
的 .eml 檔案（例如同事本機收到、手動存成 .eml 的信）。

跟 Verse 版本的關鍵差異：
- 沒有 Domino UNID（沒有即時爬蟲攔截 OpenDocument 請求這回事）——document_id
  改用信件本身 Message-ID 的雜湊值（每個 .eml 檔頭都有唯一的 Message-ID，一樣
  具備 idempotent 特性）
- 沒有「訊息級拆分」這件事——每個 .eml 檔案本身就是一則完整訊息（Notes/Outlook
  匯出時，較早的回覆歷史是用引用文字內嵌在同一個 body 裡，不是像 Verse 分組
  討論串那樣可以逐則展開），RAG/Hindsight 一樣要用 quote_stripper 砍掉引用歷史，
  但不用像 Verse 那樣逐一 accordion 展開
- 分支 B（EML/Gmail）不用重新組 .eml——來源檔案本身就是完整的原始信件（含
  附件、HTML、引用歷史），直接搬到 EML_OUTPUT_DIR/Undo（用 document_id 重新
  命名，之後跟 verse_archive_pipeline.py 產生的檔案共用同一個上傳佇列），不用
  像 Verse 那樣從爬蟲抓到的文字重新 pack_eml()
- 沒有「移到 domdom」這個資料夾動作——原始 .eml 直接搬進 EML_OUTPUT_DIR/Undo，
  等 verse_upload_gmail.py 上傳成功後自然搬進 EML_OUTPUT_DIR/Done，跟現有共用
  Undo/Done 池的慣例一致，不另外設計一套「已處理」資料夾

用法：
    python eml_folder_archive_pipeline.py [max_results] [--no-move] [--proj-tag TAG]

    max_results   處理上限（一個 .eml 檔案算一封），預設處理資料夾內全部
    --no-move     只做 RAG/Hindsight + 附件另存 + 會議記錄/報價單分類，不搬移
                   原始 .eml、不放進 Undo（測試用，只處理前 1 封）
    --proj-tag    Hindsight tags 額外加的 proj 標籤（預設見 PROJ_TAG 常數）

環境變數：
    EML_FOLDER_SOURCE_DIR   來源資料夾（預設見 SOURCE_DIR 常數）
    EML_FOLDER_PROJ_TAG     同 --proj-tag，命令列參數優先
    HINDSIGHT_URL / QDRANT_URL / EMBEDDING_API_BASE / EML_OUTPUT_DIR
        跟 verse_archive_pipeline.py 共用同一組環境變數與預設值
"""
import os, sys, re, json, hashlib, tempfile, shutil, warnings, socket
from datetime import datetime
import email
import email.policy
import email.header
from email.utils import getaddresses, parsedate_to_datetime
import requests
from bs4 import BeautifulSoup
warnings.filterwarnings('ignore')

socket.setdefaulttimeout(60)

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quote_stripper import strip_quoted_history
from email_mapping import email_to_name
from external_contacts_tracker import (
    load_state as load_contacts_state,
    save_state as save_contacts_state,
    track_unknown_contact,
    has_new_or_updated as contacts_have_new_or_updated,
)
from external_contacts_excel import generate_excel as generate_contacts_excel
from meeting_quote_upload import process_meeting_quote_attachments

# ── Hindsight（同 verse_archive_pipeline.py 的最小 client，行為需保持一致）──────
HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")


class HindsightClient:
    def __init__(self, url):
        self.url = url
        resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "eml-folder-archive", "version": "1.0"}},
        }, timeout=30)
        self.session_id = resp.headers.get("mcp-session-id")

    def retain(self, content, document_id, timestamp, metadata, context, bank_id="EID", tags=None):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "retain", "arguments": {
                "content": content, "document_id": document_id,
                "timestamp": timestamp,
                "metadata": metadata, "context": context,
                "bank_id": bank_id, "tags": tags,
            }},
        }, headers={"mcp-session-id": self.session_id}, timeout=30)
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                return json.loads(line[5:])
        return {}


# ── 設定（跟 verse_archive_pipeline.py 共用同一組環境變數）───────────────────────
QDRANT_URL = os.environ.get("QDRANT_URL", "http://10.11.1.40:6333")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "http://localhost:8081/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "jina-embed")
COLLECTION = "verse_emails"
VECTOR_SIZE = 2048
OUTPUT_FILE = os.path.join(tempfile.gettempdir(), "eml_folder_archive_pipeline_result.json")

SOURCE_DIR = os.environ.get(
    "EML_FOLDER_SOURCE_DIR",
    r"\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\YCMU-EML",
)
EML_OUTPUT_DIR = os.environ.get(
    "EML_OUTPUT_DIR", r"\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\eml")
EML_UNDO_DIR = os.path.join(EML_OUTPUT_DIR, "Undo")
ATTACHMENTS_DIR = os.path.join(EML_OUTPUT_DIR, "attachments")
os.makedirs(EML_UNDO_DIR, exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

GOOGLE_CHAT_SPACES = {
    "shuhsing": "h2YgpyAAAAE",
    "tzuyu": "8DyTYKAAAAE",
    "ycmu": "5tOqwKAAAAE",
}
HCL_USERNAME = os.environ.get("HCL_USERNAME", "shuhsing")
NOTIFY_SPACE = GOOGLE_CHAT_SPACES.get(HCL_USERNAME, "h2YgpyAAAAE")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOTIFY_SCRIPT = os.path.join(
    _PROJECT_ROOT, "commands", "hcl-notes-approval", "scripts", "hcl_write_hindsight.py")

EXTERNAL_CONTACTS_XLSX = os.path.join(os.path.expanduser("~/verse-export"), "external_contacts.xlsx")
os.makedirs(os.path.dirname(EXTERNAL_CONTACTS_XLSX), exist_ok=True)

# ── 參數解析 ─────────────────────────────────────────────────────────────────
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
_flags = {a for a in sys.argv[1:] if a.startswith("--")}
MAX_RESULTS = int(_args[0]) if _args else None
NO_MOVE = "--no-move" in _flags
_proj_idx = sys.argv.index("--proj-tag") if "--proj-tag" in sys.argv else None
PROJ_TAG = (
    sys.argv[_proj_idx + 1] if _proj_idx is not None and _proj_idx + 1 < len(sys.argv)
    else os.environ.get("EML_FOLDER_PROJ_TAG", "永光四廠JSR_B棟HVM量產產線建置")
)

qdrant = QdrantClient(url=QDRANT_URL, timeout=30)


def _fresh_qdrant():
    """已知不能重用長壽命 client（Qdrant 端會悄悄關閉閒置 keep-alive 連線造成無限期
    卡死，詳見 verse_archive_pipeline.py 的說明），每次呼叫都開一支新的。"""
    return QdrantClient(url=QDRANT_URL, timeout=30)


openai_client = OpenAI(api_key=OPENAI_KEY or "local-no-key-needed", base_url=EMBEDDING_API_BASE, timeout=60)


def ensure_collection():
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        qdrant.create_collection(
            COLLECTION, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE))
        print(f"建立 Qdrant collection: {COLLECTION}")


def id_to_uuid(h):
    h = h.ljust(32, "0")[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def already_indexed(doc_id):
    """同 verse_archive_pipeline.py：查不到/查詢失敗視為「還沒索引過」，讓後面正常寫入。"""
    try:
        points = _fresh_qdrant().retrieve(collection_name=COLLECTION, ids=[id_to_uuid(doc_id)])
        return len(points) > 0
    except Exception:
        return False


try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None

EMBED_TOKEN_LIMIT = 8000


def get_embedding(text):
    if _ENC is not None:
        toks = _ENC.encode(text)
        if len(toks) > EMBED_TOKEN_LIMIT:
            text = _ENC.decode(toks[:EMBED_TOKEN_LIMIT])
    elif len(text) > EMBED_TOKEN_LIMIT * 2:
        text = text[:EMBED_TOKEN_LIMIT * 2]
    res = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return res.data[0].embedding


# ── .eml 解析 ────────────────────────────────────────────────────────────────
def make_doc_id(message_id, subject, from_addr, date_str):
    """document_id：優先用信件本身的 Message-ID（每個 .eml 檔頭都有，唯一且
    idempotent）；缺 Message-ID 時退回 hash(寄件人|主旨|日期) 當備援。"""
    basis = message_id or f"{from_addr}|{subject}|{date_str}"
    return hashlib.md5(basis.encode("utf-8", errors="replace")).hexdigest()


def normalize_sent_date(dt):
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def _decode_bytes_with_fallback(data, charset):
    """實測發現（永光四廠 JSR 相關信件，跟日本 JSR 公司往來的轉寄信）：部分郵件
    用戶端宣告 charset=gb2312，但實際位元組其實是 GBK/GB18030（gb2312 的超集，
    含更多字元，常見於舊版中文郵件用戶端的已知寬鬆編碼行為）。Python email 模組
    內建解法（policy.default 的 get_content()/get('Subject')）遇到 gb2312 解不了
    的位元組會直接吃掉、整段換成 U+FFFD，且從那之後拿到的字串已經是損毀過的，
    無法回頭修——實測 121 封裡有 12 封主旨或內文因此亂碼。這裡改成先試超集
    （gb18030/gbk），失敗才退回宣告的 charset，都失敗才用 errors='replace'。"""
    charset = (charset or 'utf-8').lower().replace('_', '-')
    candidates = [charset]
    if charset in ('gb2312',):
        candidates = ['gb18030', 'gbk', charset]
    elif charset in ('big5',):
        candidates = ['cp950', charset]
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(charset, errors='replace')


def decode_header_value(raw_header):
    """手動解 RFC2047 encoded-word 表頭（subject/顯示名），套用
    _decode_bytes_with_fallback() 的超集重試，取代 email 模組內建、charset
    標錯時會直接吃字產生 U+FFFD 的解法。呼叫端要傳「未經 policy.default 解碼過」
    的原始表頭字串（例如用 email.policy.compat32 剖析取得），不能傳
    policy.default 已經解碼過（可能已經損毀）的字串。"""
    if not raw_header:
        return ""
    try:
        parts = email.header.decode_header(raw_header)
    except Exception:
        return raw_header
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(_decode_bytes_with_fallback(data, charset))
        else:
            out.append(data)
    return "".join(out)


def get_text_content(msg):
    """優先用 text/plain；只有 text/html 時剝標籤轉純文字。跳過標記
    Content-Disposition: attachment 的部分（那些是附件，不是內文）。手動取
    raw bytes 再用 _decode_bytes_with_fallback() 解碼（不用 part.get_content()，
    原因同 decode_header_value() 說明——charset 標錯時內建解法會直接吃字）。"""
    plain = html_text = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        cd = (part.get("Content-Disposition") or "").lower()
        if "attachment" in cd:
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = _decode_bytes_with_fallback(payload, part.get_content_charset())
        except Exception:
            continue
        if ct == "text/plain" and plain is None:
            plain = text
        elif ct == "text/html" and html_text is None:
            html_text = text
    if plain and plain.strip():
        return plain.strip()
    if html_text:
        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    return ""


def get_attachments(msg):
    """只取真正的附件（Content-Disposition: attachment），排除內嵌在 HTML 裡的
    小圖示（inline，例如 Notes 簽名檔的 ecblank.gif/doclink.gif）。"""
    atts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        cd = part.get("Content-Disposition") or ""
        if "attachment" not in cd.lower():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:
            data = None
        if data is None:
            continue
        atts.append((filename, data))
    return atts


def safe_attachment_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().strip('.')
    return (cleaned or "attachment")[:150]


def save_attachments(attachments, doc_id):
    saved = []
    for name, data in attachments:
        fname = f"{doc_id}_{safe_attachment_filename(name)}"
        path = os.path.join(ATTACHMENTS_DIR, fname)
        with open(path, 'wb') as f:
            f.write(data)
        saved.append({"name": name, "path": path})
    return saved


def _strip_wrapping_quotes(name):
    """部分外部廠商（實測：futai.com.tw）信頭顯示名本身用單引號包起來
    （例如 `'廖俊賢 Jason Liao' <jasonliao@futai.com.tw>`）——這不是 RFC 5322
    合法的引號字元，getaddresses() 不會幫忙拆掉，原樣保留在 display name 裡，
    這裡單純去掉這種包住整個名字的單引號，只是顯示美觀，不影響比對邏輯。"""
    name = (name or '').strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].strip()
    return name


def resolve_person(addr, display_name, contacts_state=None, date_str=None):
    """回傳 (email, name)。addr 是實際 email（或匯出信件裡少數 Domino canonical
    name 混雜 email 的怪格式，見已知缺口），display_name 是信頭裡的顯示名。
    email_mapping 查不到時，用顯示名頂著，並記進未知聯絡人追蹤（跟
    verse_archive_pipeline.py 的 resolve_sender() 邏輯一致）。"""
    display_name = _strip_wrapping_quotes(display_name)
    if not addr:
        return '', display_name or ''
    name = email_to_name(addr)
    if name != addr:
        return addr, name
    if contacts_state is not None and '@' in addr:
        track_unknown_contact(addr, display_name, date_str, contacts_state)
    return addr, (display_name or addr)


def resolve_recipient_list(raw_header_values, contacts_state=None, date_str=None):
    """raw_header_values: 從 email.policy.compat32 剖析取得的 msg.get_all('To')/
    msg.get_all('Cc')（未經 policy.default 解碼，見 decode_header_value() 說明）。
    用 email.utils.getaddresses() 正確處理多收件人（不用像 Verse 掃描文字那樣自己
    切逗號），每個顯示名再用 decode_header_value() 解碼，回傳解析成姓名、用
    「、」串接的字串（跟 Verse 版 resolve_recipients() 輸出格式一致）。"""
    if not raw_header_values:
        return ""
    pairs = getaddresses(raw_header_values)
    names = []
    for display, addr in pairs:
        if not addr and not display:
            continue
        display = decode_header_value(display)
        _, name = resolve_person(addr, display, contacts_state, date_str)
        names.append(name)
    return "、".join(names)


def parse_eml_file(path):
    with open(path, 'rb') as f:
        raw_bytes = f.read()
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    # compat32：headers 不會被自動解碼（保留原始 RFC2047 encoded-word 字串），
    # 供 decode_header_value() 自己套用 charset 超集重試——email.policy.default
    # 的自動解碼在 charset 標錯時會直接吃字變成 U+FFFD 且無法回頭修，body/結構
    # 解析（is_multipart/get_payload 等）不受影響，仍用上面的 msg（policy.default）。
    msg_raw = email.message_from_bytes(raw_bytes, policy=email.policy.compat32)

    subject = decode_header_value(msg_raw.get('Subject') or '') or '(no subject)'
    message_id = msg.get('Message-ID') or ''
    from_pairs = getaddresses([msg_raw.get('From') or ''])
    from_display_raw, from_addr = (from_pairs[0] if from_pairs else ('', ''))
    from_display = decode_header_value(from_display_raw)

    date_hdr = msg.get('Date')
    sent_dt = None
    if date_hdr:
        try:
            sent_dt = parsedate_to_datetime(date_hdr)
        except Exception:
            sent_dt = None
    sent_date = normalize_sent_date(sent_dt)

    body_raw = get_text_content(msg)
    body_clean = strip_quoted_history(body_raw)
    attachments = get_attachments(msg)

    return {
        "raw_bytes": raw_bytes,
        "subject": subject,
        "message_id": message_id,
        "from_display": from_display,
        "from_addr": from_addr,
        "to_raw": msg_raw.get_all('To') or [],
        "cc_raw": msg_raw.get_all('Cc') or [],
        "date_hdr": date_hdr or "",
        "sent_date": sent_date,
        "body": body_clean,
        "attachments": attachments,
    }


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main():
    ensure_collection()
    hindsight = HindsightClient(HINDSIGHT_URL)

    if not os.path.isdir(SOURCE_DIR):
        print(f"✗ 找不到來源資料夾：{SOURCE_DIR}")
        sys.exit(1)

    all_files = sorted(
        f for f in os.listdir(SOURCE_DIR) if f.lower().endswith('.eml'))
    if MAX_RESULTS:
        all_files = all_files[:MAX_RESULTS]
    if NO_MOVE:
        all_files = all_files[:1]

    print(f"來源：{SOURCE_DIR}")
    print(f"共 {len(all_files)} 封待處理" + (f"（proj tag: {PROJ_TAG}）" if PROJ_TAG else "")
          + (" [--no-move 測試模式，只處理第 1 封]" if NO_MOVE else ""))

    contacts_state_before = load_contacts_state()
    contacts_state = json.loads(json.dumps(contacts_state_before))

    results = []
    for i, fname in enumerate(all_files, 1):
        path = os.path.join(SOURCE_DIR, fname)
        rec = {"file": fname}
        try:
            parsed = parse_eml_file(path)
        except Exception as e:
            rec["error"] = f"parse failed: {e}"
            print(f"[{i}/{len(all_files)}] ✗ 解析失敗：{fname[:50]} — {e}")
            results.append(rec)
            continue

        subject = parsed["subject"]
        doc_id = make_doc_id(parsed["message_id"], subject, parsed["from_addr"], parsed["sent_date"])
        rec["doc_id"] = doc_id
        rec["subject"] = subject
        rec["sent_date"] = parsed["sent_date"]

        from_email, from_name = resolve_person(
            parsed["from_addr"], parsed["from_display"], contacts_state, parsed["sent_date"])
        to_names = resolve_recipient_list(parsed["to_raw"], contacts_state, parsed["sent_date"])
        cc_names = resolve_recipient_list(parsed["cc_raw"], contacts_state, parsed["sent_date"])

        saved_attachments = save_attachments(parsed["attachments"], doc_id)
        rec["attachments"] = [a["name"] for a in saved_attachments]

        try:
            mq_records = process_meeting_quote_attachments(
                doc_id, subject, from_name, parsed["sent_date"],
                parsed["attachments"], body=parsed["body"])
            for r in mq_records:
                flag = "✓" if r["saved"] else "✗"
                print(f"    {flag} 會議記錄/報價單附件[{','.join(r['labels'])}] {r['name'][:40]}"
                      f" -> 已存檔待批次處理" + (f"（{r.get('error','')[:120]}）" if r.get("error") else ""))
        except Exception as e:
            print(f"  ⚠️ 會議記錄/報價單附件處理失敗（不影響信件本身寫入）：{e}")

        rag_ok = hindsight_ok = 0
        if len(parsed["body"]) < 3:
            rec["skipped_empty_body"] = True
        elif already_indexed(doc_id):
            rec["skipped_dup"] = True
            print(f"  ↷ 已存在（doc_id={doc_id}），跳過不寫入：{subject[:40]}")
        else:
            try:
                text = f"{subject} {parsed['body']}"
                embedding = get_embedding(text)
                _fresh_qdrant().upsert(collection_name=COLLECTION, points=[PointStruct(
                    id=id_to_uuid(doc_id),
                    vector=embedding,
                    payload={
                        "subject": subject, "body": parsed["body"],
                        "from_email": from_email, "from_name": from_name,
                        "to": to_names, "cc": cc_names,
                        "date": parsed["date_hdr"], "sent_date": parsed["sent_date"],
                        "unid": doc_id,
                        "attachments": saved_attachments,
                    })])
                rag_ok = 1
            except Exception as e:
                print(f"  ✗ RAG 失敗：{e}")

            try:
                metadata = {
                    "subject": subject, "from_email": from_email, "from_name": from_name,
                    "to": to_names, "unid": doc_id, "sent_date": parsed["sent_date"],
                }
                result = hindsight.retain(
                    content=(
                        f"主旨：{subject}\n寄件者：{from_name}"
                        + (f" <{from_email}>" if from_email else "")
                        + f"\n日期：{parsed['sent_date']}\n\n{parsed['body']}"
                    ),
                    document_id=doc_id,
                    timestamp=parsed["sent_date"],
                    metadata=metadata,
                    tags=(["mail", f"proj:{PROJ_TAG}"] if PROJ_TAG else ["mail"]),
                    context=f"信件：主旨「{subject}」，寄件者 {from_name}",
                )
                result_text = result.get("result", {}).get("content", [{}])[0].get("text", "")
                if "validation error" in result_text.lower():
                    print(f"  ✗ Hindsight 被拒絕：{result_text[:200]}")
                else:
                    hindsight_ok = 1
            except Exception as e:
                print(f"  ✗ Hindsight 失敗：{e}")

        rec["rag_ok"] = rag_ok
        rec["hindsight_ok"] = hindsight_ok

        # 分支 B：原始檔案本身就是完整信件（含附件/HTML/引用歷史），不用重新組
        # .eml——直接搬到共用 Undo 佇列，用 doc_id 重新命名（跟 Verse 分支共用同一個
        # 上傳佇列/慣例）。--no-move 測試模式不搬移，保留原始檔案方便重跑測試。
        if NO_MOVE:
            rec["move"] = "skipped (--no-move)"
            print(f"[{i}/{len(all_files)}] ✓ {subject[:40]} "
                  f"(RAG={'skip' if rec.get('skipped_dup') else rag_ok}, "
                  f"Hindsight={'skip' if rec.get('skipped_dup') else hindsight_ok}, "
                  f"附件{len(rec['attachments'])}, 不移動)")
        else:
            try:
                dest = os.path.join(EML_UNDO_DIR, f"{doc_id}.eml")
                shutil.move(path, dest)
                rec["move"] = "moved_to_undo"
                rec["undo_path"] = dest
                print(f"[{i}/{len(all_files)}] ✓ {subject[:40]} "
                      f"(RAG={'skip' if rec.get('skipped_dup') else rag_ok}, "
                      f"Hindsight={'skip' if rec.get('skipped_dup') else hindsight_ok}, "
                      f"附件{len(rec['attachments'])}, moved)")
            except Exception as e:
                rec["move"] = f"error: {e}"
                print(f"  ✗ 搬移失敗（原始檔留在來源資料夾）：{e}")

        results.append(rec)

    rag_ok_total = sum(r.get("rag_ok", 0) for r in results)
    hindsight_ok_total = sum(r.get("hindsight_ok", 0) for r in results)
    skipped_dup_total = sum(1 for r in results if r.get("skipped_dup"))
    moved_total = sum(1 for r in results if r.get("move") == "moved_to_undo")
    summary = {
        "source": SOURCE_DIR, "proj_tag": PROJ_TAG, "no_move": NO_MOVE,
        "archived_date": datetime.now().strftime("%Y-%m-%d"),
        "processed": len(results),
        "rag_ok": rag_ok_total, "hindsight_ok": hindsight_ok_total,
        "skipped_dup": skipped_dup_total, "moved": moved_total,
        "eml_undo_dir": EML_UNDO_DIR,
        "emails": results,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 完成：處理 {len(results)} 封，RAG {rag_ok_total} / Hindsight {hindsight_ok_total} 成功，"
          f"跳過重複 {skipped_dup_total}，搬移 {moved_total} 封 → {EML_UNDO_DIR}")
    print(f"  結果已寫入 {OUTPUT_FILE}")

    if NO_MOVE:
        print("  （--no-move 測試模式：不寫入未知聯絡人 state、不發 Google Chat 通知）")
        return
    if contacts_state != contacts_state_before:
        save_contacts_state(contacts_state)
    if contacts_have_new_or_updated(contacts_state_before, contacts_state):
        n_pending = generate_contacts_excel(contacts_state, EXTERNAL_CONTACTS_XLSX)
        print(f"  外部聯絡人待確認清單更新：{n_pending} 位，已寫入 {EXTERNAL_CONTACTS_XLSX}")
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md")
            changed = []
            for e, info in contacts_state.items():
                if info.get("confirmed"):
                    continue
                old = contacts_state_before.get(e)
                if old is None or set(info.get("seen_names", [])) != set(old.get("seen_names", [])) \
                        or info.get("count") != old.get("count"):
                    changed.append((e, info))
            lines = [f"📋 .eml 資料夾歸檔發現 {len(changed)} 位未在通訊錄的聯絡人待確認姓名：", ""]
            for e, info in changed:
                names = "、".join(info.get("seen_names", []))
                lines.append(f"- {e}（{names}，共 {info.get('count')} 次）")
            lines.append("")
            lines.append(f"請開啟 {EXTERNAL_CONTACTS_XLSX} 填寫 canonical_name 欄位，填完跟我說一聲。")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            import subprocess
            result = subprocess.run(
                [sys.executable, NOTIFY_SCRIPT, "--notify-only",
                 "--notify-file", tmp_path, "--space", NOTIFY_SPACE],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            os.unlink(tmp_path)
            print(f"  Google Chat 通知：{'已發送' if result.returncode == 0 else '失敗'}")
        except Exception as e:
            print(f"  ✗ Google Chat 通知失敗：{e}")


if __name__ == "__main__":
    main()
