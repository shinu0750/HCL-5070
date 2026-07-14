#!/usr/bin/env python3
"""
HCL Verse 歸檔 pipeline
======================
從「04Done」資料夾逐封處理：抓全文+附件 → 建 RAG 索引(Qdrant) + 存成 .eml
→ 處理完移到「domdom」資料夾（移出來源 = 天然去重游標）。

兩種用途、兩份不同粒度的資料，互不影響：
- RAG(Qdrant)/Hindsight：討論串（thread）拆成「訊息級」處理，每則訊息各自用 Domino
  UNID 當 document_id（不是 hash 畫面文字），各自獨立一筆；每則的引用歷史
  （quote-in-body）會被截斷，避免重複內容灌爆 Hindsight。不記錄 thread_id/
  reply_to_unid 這種跨訊息關聯——每天執行時，同一討論串較早的訊息通常前幾天
  就已經歸檔並移出 Verse，當下這一批根本看不到完整討論串，硬要配對只會得到
  不完整、誤導性的關聯，之後如果真的需要，應該用後處理（從已存進去的資料反查）
  而不是在歸檔當下猜
- EML：整封信/整串完整存檔（不截斷、不砍引用），保留信件原貌，供人工回溯查閱、
  上傳 Gmail 用；同理不組 In-Reply-To/References，每則訊息在 Gmail 都是獨立的信

用法：
    python3 verse_archive_pipeline.py [max_results] [--no-move] [--headful] [--by-messages]

    max_results     處理上限，預設 50
    --no-move       只做 EML+RAG，不移動信件（測試用，不會動到信箱）
    --headful       顯示瀏覽器視窗（除錯用；預設 headless）
    --by-messages   max_results 改成「訊息數」上限（累計到達即停），而不是預設的
                    「信件/列數」上限——討論串會拆成多則訊息，一封信可能不只一則
"""
import os, tempfile, sys, re, json, hashlib, warnings, urllib.parse, subprocess, socket
from datetime import datetime, timedelta
import requests
from email.message import EmailMessage
import email.policy
from email.utils import formatdate
warnings.filterwarnings('ignore')  # 關閉內部 SSL 憑證警告

# 實測發現：對 QdrantClient(timeout=30) 這種 client 層級的 timeout 參數沒有生效
# （卡在 10.11.1.40:6333 的 CloseWait 連線，超過 30 秒還是沒有拋出例外）——很可能
# 是連線池重用了一條已經被對方關閉的 keep-alive 連線，request 層級的 timeout 沒有
# 正確套用到這個底層 socket 上。改用 process 全域的 socket timeout 當最後一道防線，
# 不管哪一個 library/呼叫沒有正確處理自己的 timeout，底層 socket 卡超過這個秒數
# 都會直接拋 socket.timeout，不會再無限期卡死整支 pipeline。
socket.setdefaulttimeout(60)

# Windows 主控台預設用 cp950（Big5），印不出 ✓/✗ 等符號會直接 UnicodeEncodeError 崩潰
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

from playwright.sync_api import sync_playwright
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser("~/Claude/HCL"))
from quote_stripper import strip_quoted_history
from email_mapping import email_to_name, resolve_me
from external_contacts_tracker import (
    load_state as load_contacts_state,
    save_state as save_contacts_state,
    track_unknown_contact,
    has_new_or_updated as contacts_have_new_or_updated,
)
from external_contacts_excel import generate_excel as generate_contacts_excel
from meeting_quote_upload import process_meeting_quote_attachments

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")


class HindsightClient:
    def __init__(self, url):
        self.url = url
        resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "verse-archive", "version": "1.0"}},
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

# ── 設定 ───────────────────────────────────────────────────────────────────
PORTAL_URL    = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL     = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME      = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD      = os.environ.get("HCL_PASSWORD",    "")
QDRANT_URL    = os.environ.get("QDRANT_URL",      "http://10.11.1.40:6333")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY",  "")
# 本地 llama-cpp-server 跑 jina-embeddings-v4（CPU），OpenAI-compatible API，不需要真的 OpenAI key
# 8081 是 systemd on-demand socket（jina-embed.socket），沒人用時自動關掉背後的
# llama-server 省 RAM，一有請求會自動喚醒——不要接 8090，那是背後 backend 本身的
# port，閒置 ~10 分鐘會被 idle-watchdog 關掉，長時間跑 pipeline 中途會斷線
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "http://localhost:8081/v1")
EMBEDDING_MODEL    = os.environ.get("EMBEDDING_MODEL",    "jina-embed")

SOURCE_FOLDER = os.environ.get("VERSE_SOURCE_FOLDER", "04Done")
TARGET_FOLDER = os.environ.get("VERSE_TARGET_FOLDER", "domdom")
# 一次性測試用：來源資料夾本身已經是人工分類過的專案信件時，可用這個環境變數
# 直接標記 proj tag（例如 VERSE_PROJ_TAG=JSR量產建置）。proj 分類 backfill 腳本本身
# 仍暫緩（見 SKILL.md「proj 分類（暫緩）」），這只是先接受這次已知的手動分類結果，
# 不是重新啟用自動判斷
PROJ_TAG      = os.environ.get("VERSE_PROJ_TAG", "").strip() or None
COLLECTION    = "verse_emails"
# 實測發現 jina-embeddings-v4 實際回傳 2048 維（不是原本假設的 1024），
# Qdrant collection 已重建成 2048 維，這裡要同步，否則 upsert 100% 失敗
VECTOR_SIZE   = 2048
OUTPUT_FILE   = os.path.join(tempfile.gettempdir(), "verse_archive_pipeline_result.json")

MY_EMAIL, MY_NAME = resolve_me(USERNAME)  # 目前登入帳號 -> (email, 姓名)，取代寫死 shuhsing

# ── 參數解析 ─────────────────────────────────────────────────────────────────
_args     = [a for a in sys.argv[1:] if not a.startswith("--")]
_flags    = {a for a in sys.argv[1:] if a.startswith("--")}
MAX_RESULTS = int(_args[0]) if _args else 50
NO_MOVE     = "--no-move" in _flags
HEADFUL     = "--headful" in _flags
# --by-messages：MAX_RESULTS 改成「訊息數」上限（累計到達即停），而不是預設的
# 「信件/列數」上限——討論串會拆成多則訊息，用信件數當上限沒辦法精準控制訊息總數
BY_MESSAGES = "--by-messages" in _flags
OUTPUT_DIR  = os.path.expanduser("~/verse-export")
os.makedirs(OUTPUT_DIR, exist_ok=True)
EXTERNAL_CONTACTS_XLSX = os.path.join(OUTPUT_DIR, "external_contacts.xlsx")
# 分支 B（EML + 附件）實際存放位置：部門共用網路磁碟，不是本機 ~/verse-export
# （本機那份只留給 xlsx 這些還沒決定搬過去的東西）
EML_OUTPUT_DIR = os.environ.get(
    "EML_OUTPUT_DIR",
    r"\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\eml",
)
os.makedirs(EML_OUTPUT_DIR, exist_ok=True)
# 新產生的 .eml 一律先放共用資料夾底下的 Undo 子目錄（不分帳號各自存），
# 等 verse_upload_gmail.py 上傳成功後再搬到同一個共用資料夾底下的 Done
# （見 SKILL.md「已知缺口」：之前用「各帳號本機 eml_done」設計，導致共用
# Undo 池裡誰的信件都混在一起，很難看出目前累積了哪些人的哪些信還沒上傳）
EML_UNDO_DIR = os.path.join(EML_OUTPUT_DIR, "Undo")
os.makedirs(EML_UNDO_DIR, exist_ok=True)
# 附件另存一份（跟 .eml 放同一個網路資料夾底下的子目錄），檔名前綴 unid 避免同名衝突
ATTACHMENTS_DIR = os.path.join(EML_OUTPUT_DIR, "attachments")
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

# 每個帳號對應的 Google Chat space（沿用 hcl-notes-approval 的「使用者對照表」）
GOOGLE_CHAT_SPACES = {
    "shuhsing": "h2YgpyAAAAE",
    "tzuyu":    "8DyTYKAAAAE",
    "ycmu":     "5tOqwKAAAAE",
}
NOTIFY_SPACE = GOOGLE_CHAT_SPACES.get(USERNAME, "h2YgpyAAAAE")
# 專案根目錄（.../HCL），從這支腳本的路徑往上推 4 層算出來，找 hcl_write_hindsight.py 用
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
NOTIFY_SCRIPT = os.path.join(
    _PROJECT_ROOT, ".claude", "commands", "hcl-notes-approval", "scripts", "hcl_write_hindsight.py")


# 實測遇過：NAS 那端連線斷過一次（TCP CloseWait），QdrantClient 預設不設 timeout，
# 底層 request 會對著一個死掉的連線永久卡死（不是拋例外，是真的 hang，已有的
# try/except 完全救不到，因為它從來沒有機會執行到 except）。設 timeout=30 跟
# 全域 socket.setdefaulttimeout() 兩層防護都試過，實測還是會卡超過 60 秒以上不吐
# 例外——研判是 keep-alive 連線池重用了一條「批次跑到一半、閒置一陣子後被 NAS
# 端悄悄關掉」的舊連線，client 層/socket 層的 timeout 設定都套用不到這個殭屍連線
# 上。已用真實批次跑 3 次重現（不是偶發）；改成每次呼叫都開一支全新的
# QdrantClient（見 _fresh_qdrant()）才能繞開這個問題，跟手動 curl 測試每次都是
# 新連線、每次都秒回是同一個道理。
qdrant        = QdrantClient(url=QDRANT_URL, timeout=30)


def _fresh_qdrant():
    """已知不能重用長壽命的 qdrant client（見上面說明），每次呼叫都開一支新的，
    避免連線池裡的殭屍連線造成無限期卡死。"""
    return QdrantClient(url=QDRANT_URL, timeout=30)
# timeout=60：本地 embedding server 是 on-demand socket 喚醒架構，冷啟動要等
# backend 醒過來（正常幾秒到十幾秒），沒設 timeout 的話一旦網路/backend 有異常
# 會無限期卡住（跟下面 qdrant/attachment 下載是同一類問題）
openai_client = OpenAI(api_key=OPENAI_KEY or "local-no-key-needed", base_url=EMBEDDING_API_BASE, timeout=60)


# ── Qdrant / embedding / id ────────────────────────────────────────────────
def ensure_collection():
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        qdrant.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"建立 Qdrant collection: {COLLECTION}")


def make_id(subject, sender, date):
    """訊息級 UNID 抓不到時的備援 id（不理想，僅防止整批失敗）。"""
    return hashlib.md5(f"{sender}|{subject}|{date}".encode()).hexdigest()


def already_indexed(unid):
    """這個 UNID 是否已經寫進 Qdrant。已實測驗證 UNID 跨帳號一致（同一則訊息在
    不同人信箱裡的 UNID 相同），所以之後合併同事信箱的信件時，同一則訊息重複
    遇到直接跳過整段 RAG+Hindsight 寫入，不要再用 upsert 蓋掉——upsert 雖然本身
    idempotent，但如果 Hindsight 那筆記錄事後被人工整理過，重新 retain 會把整理
    過的內容蓋掉，跳過寫入才安全。查詢失敗（例如 collection 還不存在）視為
    「還沒索引過」，讓後面正常走寫入流程。"""
    try:
        points = _fresh_qdrant().retrieve(collection_name=COLLECTION, ids=[id_to_uuid(unid)])
        return len(points) > 0
    except Exception:
        return False


def make_row_signature(subject, sender, snippet):
    """安全閥用的『這一列信件』簽章，跟訊息級 id 無關。"""
    return hashlib.md5(f"{sender}|{subject}|{snippet}".encode()).hexdigest()


def id_to_uuid(h):
    h = h.ljust(32, "0")[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")  # text-embedding-3-* 用的編碼
except Exception:
    _ENC = None

EMBED_TOKEN_LIMIT = 8000  # 模型上限 8192，留緩衝

def get_embedding(text):
    # 長討論串可能超過 8192 token 上限 → 精準截斷（tiktoken 不可用時用字元數粗估）
    if _ENC is not None:
        toks = _ENC.encode(text)
        if len(toks) > EMBED_TOKEN_LIMIT:
            text = _ENC.decode(toks[:EMBED_TOKEN_LIMIT])
    elif len(text) > EMBED_TOKEN_LIMIT * 2:
        text = text[:EMBED_TOKEN_LIMIT * 2]
    res = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return res.data[0].embedding


# ── 身份解析（me / email <-> 姓名）──────────────────────────────────────────
def resolve_sender(raw):
    """回傳 (email, name, found_in_directory)。raw 可能是 'me'、'name <email>'、
    或純文字姓名。found_in_directory=False 代表 email_mapping 查不到這個 email
    （外部聯絡人/離職同仁），呼叫端應該把它記進待確認名單。"""
    raw = (raw or '').strip()
    if not raw:
        return '', '', True
    if raw == 'me':
        return MY_EMAIL, MY_NAME, True
    m = re.match(r'^"?([^"<>]*)"?\s*<([^<>]+)>$', raw)
    if m:
        display, addr = m.group(1).strip(), m.group(2).strip()
        name = email_to_name(addr)
        found = (name != addr)
        if not found:  # 通訊錄查不到，用畫面上的顯示名頂著
            name = display or addr
        return addr, name, found
    return '', raw, True  # 沒有 email 可查，無法追蹤，視為不需處理


def substitute_me(raw):
    """把收件人/副本字串裡的獨立 'me' 換成目前登入帳號的 email（不再寫死 shuhsing）。"""
    if not raw:
        return raw
    return re.sub(r'(?<![\w@.])me(?![\w@.])', MY_EMAIL or 'me', raw)


_ASCII_SINGLE_WORD = re.compile(r"^[A-Za-z][A-Za-z.\-']*$")


def _split_recipient_entries(raw):
    """把 to/cc 原始字串安全切成一個個收件人條目。不能單純用逗號切——西式姓名的
    'Lastname, Firstname <email>' 格式（例如 Verse 常見的「Hsieh, Tata」）本身就含
    逗號，會被誤切成兩筆（「Hsieh」變成一個沒有 email 的假收件人）。

    做法：初步用逗號切開後，如果某一段本身沒有 email/括號、且是「不含空白的純英文
    單詞」（像獨立姓氏 Hsieh、Yamashita 那樣），同時緊接著的下一段有 `<email>`，
    視為同一個人被誤切，合併回去。只處理這種「單一英文單詞」的情況——中文姓名跟
    多字英文全名（例如 'Yao-Chung Liu' 這種本身就有空白的完整姓名）不會被合併，
    降低把兩個不同人誤判成同一人的風險（這是啟發式判斷，不保證 100% 正確）。"""
    raw_parts = [p.strip() for p in raw.split(',')]
    merged, i = [], 0
    while i < len(raw_parts):
        part = raw_parts[i]
        if (part and '<' not in part and '@' not in part
                and _ASCII_SINGLE_WORD.match(part)
                and i + 1 < len(raw_parts) and '<' in raw_parts[i + 1]):
            merged.append(f"{part}, {raw_parts[i + 1]}")
            i += 2
        else:
            merged.append(part)
            i += 1
    return [p for p in merged if p]


def parse_validation_response(text):
    """解析 iNotes 姓名驗證 API（POST .../iNotes/Proxy/?EditDocument&Form=s_ValidationJson）
    的回應，回傳 {canonical_name: (中文名, email)}。

    這支 API 是 Verse 自己在各種時機（不只是我們正在處理的這封信——folder 列表
    渲染寄件人、其他信件的收件人預先驗證等等都會觸發）用來把 Domino canonical
    name（像 `Chun-Hua Huang/elfc1/everlight` 這種分廠/子公司用英文命名慣例
    註冊的帳號）解析成中文名+email。**注意**：Verse 自動觸發的這些請求內容跟我們
    正在處理的那封信的收件人不一定相關（實測發現一次自動請求裡的 70 幾個名字
    跟目標信件完全對不上，應該是 folder 列表其他信件的名字）——不能假設「打開
    這封信就會自動解析出這封信的收件人」。真正可靠的做法是用
    resolve_canonical_names_via_api() 主動幫這封信裡沒解析到的名字補送一次批次
    請求，這支函式只負責被動 parse 任何一次回應（不管是 Verse 自己觸發的還是
    我們自己送的），找到就收，找不到（真的是外部廠商查無此人）的名字直接跳過，
    留給呼叫端原樣保留顯示名。"""
    result = {}
    try:
        data = json.loads(text)
    except Exception:
        return result
    top_entries = (data.get("viewentry") or {}).get("entrydata") or []
    for section in top_entries:  # "local" / "server" 兩個區段都可能有解析結果
        section_entries = ((section.get("viewentries") or {}).get("viewentry")) or []
        for item in section_entries:
            fields = {f.get("@name"): f for f in (item.get("entrydata") or [])}
            original = ((fields.get("originalName") or {}).get("text") or {}).get("0", "")
            candidates = ((fields.get("candidate") or {}).get("viewentries") or {}).get("viewentry") or []
            if not original or not candidates:
                continue
            cand_fields = {f.get("@name"): f for f in (candidates[0].get("entrydata") or [])}
            alt_full = ((cand_fields.get("altFullName") or {}).get("text") or {}).get("0", "")
            email = ((cand_fields.get("internetAddress") or {}).get("text") or {}).get("0", "")
            m = re.match(r'CN=([^/]+)', alt_full)
            chinese_name = m.group(1).strip() if m else ""
            # server 區段通常比 local 完整，有拿到新資料就覆蓋掉舊的（含 local 那份空值）
            if chinese_name or email or original not in result:
                result[original] = (chinese_name, email)
    return result


def resolve_canonical_names_via_api(page, canonical_names, nonce):
    """在還開著、已登入的頁面 context 內，主動幫一批 Domino canonical name
    （沒有 `@` 的那種，例如 `Chun-Hua Huang/elfc1/everlight`）補送一次
    s_ValidationJson 批次驗證請求，回傳 parse_validation_response() 的結果。

    一次可以丟多個名字（分號分隔），不用一個一個點名片卡觸發。這支 API 需要
    `X-IBM-INotes-Nonce` header 才會通過（純用登入 cookies 呼叫會被 401 擋掉，
    也試過從 meta tag/window 全域變數找這個 nonce，兩者都沒有——只能從真的發生
    過的 s_ValidationJson 請求（不管是 Verse 自己觸發的還是我們自己送的）的
    request header 被動攔截取得，呼叫端要自己維護抓到的 nonce 值傳進來。
    canonical_names 為空、或還沒抓到任何 nonce 時直接回傳空字典，不發請求。"""
    if not canonical_names or not nonce:
        return {}
    names_str = ";".join(canonical_names)
    js_result = page.evaluate("""async ([namesStr, nonceVal]) => {
        const body = new URLSearchParams();
        body.set('%%PostCharset', 'UTF-8');
        body.set('VAL_NameEntries', namesStr);
        body.set('VAL_DisablePartial', '1');
        body.set('VAL_Commands', '$cache');
        try {
            const resp = await fetch(
                'https://mail1.ecic.com.tw/mail/6971.nsf/iNotes/Proxy/?EditDocument&Form=s_ValidationJson&xhr=1&sq=1',
                { method: 'POST', credentials: 'include',
                  headers: { 'Content-Type': 'application/x-www-form-urlencoded',
                             'X-Requested-With': 'XMLHttpRequest',
                             'X-IBM-INotes-Nonce': nonceVal },
                  body: body.toString() }
            );
            return { status: resp.status, text: await resp.text() };
        } catch (e) {
            return { status: 0, text: '', error: String(e) };
        }
    }""", [names_str, nonce])
    if js_result.get("status") != 200:
        return {}
    return parse_validation_response(js_result.get("text", ""))


def resolve_recipients(raw, name_canonicals=None, name_directory=None,
                        contacts_state=None, date_str=None):
    """把 to/cc 字串裡每個 'Name <email>' 或純 email 都換成通訊錄查到的姓名，
    只給 RAG/Hindsight 用（可讀性優先，不需要真的 email）。EML 那邊要保留原始
    收件人資訊（含 email），不要走這個函式——兩種輸出用途不同，見 pipeline 文件說明。

    name_canonicals：這則訊息收件人「顯示名 -> Domino canonical name」的對照
    （來自 extract_message_block() 抓的 socpimNameBtn 的 socpimnameemail 屬性）。
    name_directory：整個 pipeline run 累積的「canonical name -> (中文名, email)」
    對照表（來自 parse_validation_response() 持續攔截 Verse 自動發的驗證回應）。
    兩者都沒有（例如舊呼叫方式，或這則訊息沒有任何無 email 收件人）時行為跟改之前
    完全一樣。

    contacts_state/date_str：帶了才會追蹤「有 email 但 email_mapping 查不到」的
    收件人（呼叫 track_unknown_contact()，跟 resolve_sender() 現有機制一致，見
    changelog 3.13.0）。純姓名、沒有 email 的收件人（例如外部廠商自己的 Domino
    canonical name，格式不是我們能解析的 email）無法用這個機制追蹤——沒有 email
    可以當 key，也沒有實際 email 能寫回 email_mapping，留著原樣顯示，仍是已知缺口。"""
    if not raw:
        return raw
    name_canonicals = name_canonicals or {}
    name_directory = name_directory or {}
    names = []
    for part in _split_recipient_entries(raw):
        m = re.match(r'^"?([^"<>]*)"?\s*<([^<>]+)>$', part)
        if m:
            display, addr = m.group(1).strip(), m.group(2).strip()
            name = email_to_name(addr)
            if name == addr and contacts_state is not None:
                track_unknown_contact(addr, display, date_str, contacts_state)
            names.append(name if name != addr else (display or addr))
        elif '@' in part:
            name = email_to_name(part)
            if name == part and contacts_state is not None:
                track_unknown_contact(part, "", date_str, contacts_state)
            names.append(name if name != part else part)
        else:
            # 純姓名、沒有 email——查這則訊息抓到的 canonical name，
            # 再去 Verse 自動驗證累積出來的對照表換中文名；兩邊都查不到就原樣保留
            resolved = None
            canonical = name_canonicals.get(part.strip())
            if canonical:
                if '@' in canonical:
                    looked_up = email_to_name(canonical)
                    resolved = looked_up if looked_up != canonical else None
                else:
                    entry = name_directory.get(canonical)
                    if entry and entry[0]:
                        resolved = entry[0]
            names.append(resolved or part)
    return "、".join(names)


def resolve_unresolved_canonicals(page, name_canonicals, name_directory, nonce):
    """給定一個 block/header 的 name_canonicals（顯示名 -> canonical name/email），
    把其中還沒進 name_directory、且真的是 Domino canonical name（沒有 @）的部分，
    主動送一次 resolve_canonical_names_via_api() 補查，結果直接 merge 進
    name_directory（就地修改）。沒有 nonce 或沒有需要補查的名字時直接跳過、不發
    請求。回傳這次新解析到的筆數，方便呼叫端記錄/除錯。"""
    unresolved = sorted({
        c for c in (name_canonicals or {}).values()
        if '@' not in c and c not in name_directory
    })
    if not unresolved or not nonce:
        return 0
    newly_resolved = resolve_canonical_names_via_api(page, unresolved, nonce)
    name_directory.update(newly_resolved)
    return len(newly_resolved)


def quote_recipient_header(raw):
    """把 to/cc 字串重組成合法的 RFC 5322 位址清單，給 EML 信頭用：任何顯示名稱本身
    含逗號（例如「Hsieh, Tata」）的收件人，用雙引號把名字包起來（`"Hsieh, Tata" <email>`）。
    不這樣做的話，Gmail（或任何標準郵件軟體）解析 To:/Cc: 信頭時，逗號本來就是
    「下一個收件人開始」的意思，會把這種名字誤判成兩個收件人——一個是沒有 email
    的「Hsieh」，一個才是真正的「Tata <email>」。"""
    if not raw:
        return raw
    out = []
    for part in _split_recipient_entries(raw):
        m = re.match(r'^"?([^"<>]*?)"?\s*<([^<>]+)>$', part)
        if m:
            display, addr = m.group(1).strip(), m.group(2).strip()
            if display and ',' in display and not (display.startswith('"') and display.endswith('"')):
                out.append(f'"{display}" <{addr}>')
            else:
                out.append(part)
        else:
            out.append(part)
    return ", ".join(out)


# ── 日期正規化 ───────────────────────────────────────────────────────────────
# Verse 的 .pim-mailread-sentdate 對近期信件會省略年份（如 "Wed, Jun 10 9:13 AM"），
# 且討論串顯示的是「最新一則」的時間。把它正規化成 ISO，缺年份就推算。
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

def normalize_sent_date(raw, today=None):
    """把 Verse 顯示的日期字串正規化成 'YYYY-MM-DD HH:MM'（或 'YYYY-MM-DD'）。無法解析回 ''。"""
    if not raw:
        return ""
    today = today or datetime.now()
    s = raw.replace("\n", " ").strip()

    # 相對日：Today / Yesterday / Tomorrow
    rel = None
    if re.search(r"\bYesterday\b", s, re.I): rel = -1
    elif re.search(r"\bToday\b", s, re.I):   rel = 0
    elif re.search(r"\bTomorrow\b", s, re.I): rel = 1

    year = None
    ym = re.search(r"\b(20\d\d)\b", s)
    if ym:
        year = int(ym.group(1))

    mon = day = None
    mn = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2})\b", s, re.I)
    if mn:
        mon = _MONTHS[mn.group(1)[:3].lower()]; day = int(mn.group(2))
    else:
        num = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d\d))?\b", s)
        if num:
            mon = int(num.group(1)); day = int(num.group(2))
            if num.group(3):
                year = int(num.group(3))

    # 時間（支援 AM/PM 與中文 上午/下午）
    hh = mm = None
    tm = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
    if tm:
        hh = int(tm.group(1)); mm = int(tm.group(2))
        is_pm = bool(re.search(r"PM|下午", s, re.I))
        is_am = bool(re.search(r"AM|上午", s, re.I))
        if is_pm and hh < 12: hh += 12
        if is_am and hh == 12: hh = 0

    # 相對日優先處理
    if rel is not None and mon is None:
        d = today + timedelta(days=rel)
        mon, day, year = d.month, d.day, d.year

    if mon is None or day is None:
        return ""
    if year is None:
        cand = datetime(today.year, mon, day)
        year = today.year if cand <= today + timedelta(days=7) else today.year - 1
    try:
        dt = datetime(year, mon, day, hh or 0, mm or 0)
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M") if hh is not None else dt.strftime("%Y-%m-%d")


# ── 登入 + 進指定資料夾 ──────────────────────────────────────────────────────
def login(page):
    page.goto(PORTAL_URL)
    page.wait_for_load_state("networkidle")
    page.fill('input[type="text"], input[placeholder*="Email"], input[name*="user"]', USERNAME)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"], button:has-text("登入")')
    page.wait_for_load_state("networkidle")
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=15000)
    page.wait_for_timeout(2000)


def _expand_treeitem_by_name(page, name):
    """展開左側導覽樹中名稱符合 name 的節點，用於巢狀資料夾路徑（例如
    「工程專案 > JSR量產建置」）——子資料夾預設不在可點擊的 DOM 節點裡，
    要先展開父資料夾才點得到。

    這個 widget 是 Dojo 元件，展開行為綁在 `.folder-icon` 子元素的真實
    click 事件上（實測過：用 page.evaluate() 對 <li> 本身發 JS 合成 click()
    不會觸發展開，一定要用 Playwright 對 `.folder-icon` 做真的滑鼠點擊）。
    """
    candidates = page.locator(f'[role="treeitem"]:has-text("{name}")')
    n = candidates.count()
    for i in range(n):
        el = candidates.nth(i)
        try:
            txt = el.inner_text(timeout=1500).strip()
        except Exception:
            continue
        first_line = txt.split('\n')[0].strip()
        if first_line == name or name in first_line:
            icon = el.locator('.folder-icon').first
            try:
                icon.click(timeout=3000)
            except Exception:
                el.click(timeout=3000)
            page.wait_for_timeout(1000)
            return True
    return False


def open_folder(page, folder_name):
    """
    點擊左側資料夾樹中名為 folder_name 的資料夾，載入其信件清單。
    Inbox 有專屬 class `.inbox`；自訂資料夾（如 04Done）只能靠名字點，
    且可能藏在「資料夾 / Folders」摺疊群組裡，需先展開。

    folder_name 支援用 ">" 表示巢狀路徑（例如 "工程專案>JSR量產建置"）——
    會依序展開每一層父資料夾，才點擊最後一層。
    """
    path_parts = [p.strip() for p in folder_name.split(">") if p.strip()]
    folder_name = path_parts[-1]

    # 1) 先嘗試展開「資料夾 / Folders」群組（若存在且未展開）
    page.evaluate("""(name) => {
        const groups = [...document.querySelectorAll('[role="treeitem"], .folder-group, .nav-group')];
        for (const g of groups) {
            const t = (g.innerText || '').trim();
            if (/^(資料夾|Folders|My Folders|個人資料夾)/.test(t)) {
                const exp = g.getAttribute('aria-expanded');
                if (exp === 'false') {
                    const toggle = g.querySelector('[aria-expanded], .twisty, .expand') || g;
                    toggle.click();
                }
            }
        }
    }""", folder_name)
    page.wait_for_timeout(800)

    # 1b) 巢狀路徑：依序展開中間每一層父資料夾（最後一層留給步驟 2 點擊）
    for parent in path_parts[:-1]:
        _expand_treeitem_by_name(page, parent)

    # 2) 在左側導航區找含 folder_name 的 treeitem 並點擊（排除右側 move popup）
    candidates = page.locator(
        f'.application-frame [role="treeitem"]:has-text("{folder_name}"), '
        f'nav [role="treeitem"]:has-text("{folder_name}"), '
        f'[role="tree"] [role="treeitem"]:has-text("{folder_name}")'
    )
    n = candidates.count()
    target = None
    for i in range(n):
        el = candidates.nth(i)
        try:
            txt = el.inner_text(timeout=1500).strip()
        except Exception:
            continue
        # 取文字最接近資料夾名的（避免點到含該字的信件列或其他項目）
        first_line = txt.split('\n')[0].strip()
        if folder_name in first_line:
            target = el
            break
    if target is None and n > 0:
        target = candidates.first  # fallback

    if target is None:
        # dump 導航 DOM 供除錯
        nav_dump = page.evaluate("""() => {
            return [...document.querySelectorAll('[role="treeitem"]')]
                .map(el => (el.className || '') + ' :: ' + (el.innerText || '').split('\\n')[0].trim())
                .slice(0, 60);
        }""")
        raise RuntimeError(
            f"找不到資料夾「{folder_name}」。目前 treeitem 清單：\n  " +
            "\n  ".join(nav_dump)
        )

    target.scroll_into_view_if_needed()
    target.click()
    page.wait_for_timeout(1500)
    # 等信件清單出現（空資料夾則不會有，給較短 timeout）
    try:
        page.wait_for_selector('.seq-msg-row', timeout=8000)
    except Exception:
        pass
    # 等首列文字渲染完成（含 "Subject" 才算 ready），最多 ~6s
    for _ in range(12):
        try:
            if page.locator('.seq-msg-row').count() == 0:
                break  # 空資料夾
            t = page.locator('.seq-msg-row').first.inner_text(timeout=1500)
            if "Subject" in t:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    page.wait_for_timeout(500)


def ensure_thread_grouping_off(page, n_rows=8):
    """檢查目前資料夾前幾列有沒有討論串分組跡象（`Count N` 徽章），有的話點擊
    `[class*='toggle-threads']` 關閉。

    3.16.1 才發現：這個開關很可能不是 Verse 伺服器端的帳號設定，而是存在瀏覽器
    本地（cookie/localStorage）的偏好——`main()` 每次執行都是全新 `browser.new_
    context()`，不會保存/重用任何前次 session 的狀態，所以「之前關過」不代表這次
    還是關的，**每次執行都要重新檢查**，不能只憑上次記錄假設。使用者也明確要求
    一定要關閉分組，理由是分組信件量過多時可能造成異常，所以固定當作每次歸檔的
    必要前置步驟，不是只有被要求視覺檢查時才做。

    不管開/關，訊息級抓取邏輯本身兩種畫面下都驗證過正確（見 SKILL.md「已知缺口」），
    這裡只是照使用者要求，降低分組信件量過大時的未知風險，不是修正正確性問題。
    """
    rows = page.locator('.seq-msg-row')
    count = min(rows.count(), n_rows)
    has_count_badge = False
    for i in range(count):
        try:
            txt = rows.nth(i).inner_text(timeout=1500)
        except Exception:
            continue
        if 'Count' in txt:
            has_count_badge = True
            break
    if not has_count_badge:
        print("（討論串分組檢查：目前是關閉狀態，不用點擊）")
        return
    print("（討論串分組檢查：發現分組跡象，點擊關閉...）")
    try:
        page.click("[class*='toggle-threads']", timeout=5000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"（討論串分組切換按鈕點擊失敗，跳過：{e}）")


# ── 信件解析 / 清理（沿用 index + export 既有邏輯）────────────────────────────
UI_NOISE = {
    "More actions", "Mark as unread", "Mark as Needs Action",
    "Move to Trash", "Move to folder", "Open in new window",
    "Close", "Reply", "Reply All", "Forward", "Inbox",
    "Show more", "Show less", "Mark all as read", "Move all to Trash",
    "THREAD ACTIONS:", "Toggle message open/close",
    SOURCE_FOLDER, TARGET_FOLDER,
}

_DATE_PATS = [
    re.compile(r'^\d{1,2}:\d{2}\s*(AM|PM)$'),
    re.compile(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b'),
    re.compile(r'^(Yesterday|Today|Tomorrow)\b'),
    re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+'),
]

def is_date_line(s):
    return any(p.match(s) for p in _DATE_PATS)


def _strip_ui_noise(raw, subject):
    """剝掉 UI chrome 雜訊（Toggle/Reply/日期行/收件人行等），回傳乾淨的原始信件文字
    （還沒砍引用歷史）。"""
    lines = raw.split("\n")
    cleaned, found = [], False
    for line in lines:
        s = line.strip()
        if not found:
            if s in UI_NOISE or s == subject:
                continue
            if is_date_line(s):
                found = True
                continue
        else:
            if s in UI_NOISE:
                continue
            if re.match(r'^.{1,40}\s+to\s+(me|you)\b', s):
                continue
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def clean_body(raw, subject):
    """先剝 UI chrome 雜訊，再砍掉引用歷史（quote_stripper）。"""
    return strip_quoted_history(_strip_ui_noise(raw, subject))


def parse_msg_row(item):
    try:
        lines = [l.strip() for l in item.inner_text().strip().split("\n") if l.strip()]
        sender, subject, snippet = "", "", ""
        for i, line in enumerate(lines):
            if line == "From" and i+1 < len(lines):
                sender = lines[i+1]
            elif line == "Subject" and i+1 < len(lines):
                subject = lines[i+1]
            elif line == "Message abstract" and i+1 < len(lines):
                snippet = lines[i+1]
        if not subject:
            return None
        return {"sender": sender, "subject": subject, "snippet": snippet}
    except Exception:
        return None


def extract_header_fields(page):
    """整串（thread 級）的表頭，目前抓到的是最新/最上面那則——給 EML 用。"""
    page.evaluate("""() => {
        const c = document.querySelector('.preview-container');
        if (!c) return;
        const btn = c.querySelector('.collapsed-recipient');
        if (btn) btn.click();
    }""")
    page.wait_for_timeout(500)
    header = page.evaluate(r"""() => {
        const c = document.querySelector('.preview-container');
        if (!c) return {};
        const senderEls = [...c.querySelectorAll('.socpimMailSender')];
        let from = '';
        for (const el of senderEls) {
            const t = el.innerText.trim();
            if (t && t !== 'Sent by:' && t.includes('<')) { from = t; break; }
        }
        if (!from) {
            for (const el of senderEls) {
                const t = el.innerText.trim();
                if (t && t !== 'Sent by:') { from = t; break; }
            }
        }
        let to = '', cc = '', bcc = '';
        function parseFromTo(raw) {
            const toM  = raw.match(/To:\s*([\s\S]*?)(?:Cc:|Bcc:|Show less|$)/);
            const ccM  = raw.match(/Cc:\s*([\s\S]*?)(?:Bcc:|Show less|$)/);
            const bccM = raw.match(/Bcc:\s*([\s\S]*?)(?:Show less|$)/);
            return {
                to:  toM  ? toM[1].trim().replace(/\s+/g, ' ')  : '',
                cc:  ccM  ? ccM[1].trim().replace(/\s+/g, ' ')  : '',
                bcc: bccM ? bccM[1].trim().replace(/\s+/g, ' ') : '',
            };
        }
        const recipEl = c.querySelector('.pim-mailread-recipient');
        const toccEl  = c.querySelector('.pimToccbcc');
        const rawRecip = recipEl ? recipEl.innerText : '';
        const rawTocc  = toccEl  ? toccEl.innerText  : '';
        const rawSrc = rawRecip.includes('To:') ? rawRecip
                     : rawTocc.includes('To:')  ? rawTocc : '';
        if (rawSrc) {
            const parsed = parseFromTo(rawSrc);
            to  = parsed.to;
            cc  = parsed.cc;
            bcc = parsed.bcc;
        }
        let date = '';
        const dateEl = c.querySelector('.pim-mailread-sentdate');
        if (dateEl) {
            const lines = dateEl.innerText.split('\n').map(s => s.trim()).filter(Boolean);
            date = lines.reduce((a, b) => a.length >= b.length ? a : b, '');
        }
        const LABEL_NOISE = new Set(['Remove from 04Done', 'Remove from Inbox', 'Remove from']);
        const labelSet = new Set();
        c.querySelectorAll('.folder-chiclet').forEach(el => {
            const firstLine = el.innerText.split('\n')[0].trim();
            if (firstLine && !LABEL_NOISE.has(firstLine)) labelSet.add(firstLine);
        });

        // 同 extract_message_block()：收件人姓名的 socpimNameBtn 對照表，給
        // resolve_recipients() 保底路徑（整串沒抓到任何訊息時）用
        const nameCanonicals = {};
        [recipEl, toccEl].forEach(el => {
            if (!el) return;
            el.querySelectorAll('.socpimNameBtn').forEach(btn => {
                const t = btn.textContent.trim();
                const cn = btn.getAttribute('socpimnameemail');
                if (t && cn) nameCanonicals[t] = cn;
            });
        });

        return { from, to, cc, bcc, date, label_ids: [...labelSet], name_canonicals: nameCanonicals };
    }""")
    # 「me」換成目前登入帳號的 email（不再寫死 shuhsing）
    header["to"] = substitute_me(header.get("to", ""))
    header["cc"] = substitute_me(header.get("cc", ""))
    header["bcc"] = substitute_me(header.get("bcc", ""))
    return header


# ── 訊息級拆分：Domino UNID + 逐則抓 header/body ──────────────────────────────
UNID_RE = re.compile(r'/0/([0-9A-Fa-f]{32})/\?OpenDocument')


def open_row_and_get_block_unids(page, row_locator):
    """
    點開一列信件，同時攔截每則訊息展開時打出的 OpenDocument 請求，
    取得跟 `.preview-container .pim-mailread-container[aria-expanded]` DOM 順序一一對應的 UNID 陣列。
    UNID 是 Domino 文件本身的 id，不受帳號/資料夾/畫面顯示格式影響，
    比 hash(寄件人|主旨|日期) 可靠（跨帳號、跨資料夾重複開同一封都會拿到同一個值）。
    """
    captured = []

    def on_request(req):
        m = UNID_RE.search(req.url)
        if m:
            captured.append(m.group(1))

    page.on("request", on_request)
    try:
        row_locator.click()
        page.wait_for_timeout(2000)

        n = page.evaluate(
            "() => document.querySelectorAll('.preview-container .pim-mailread-container[aria-expanded]').length"
        )
        block_unid = [None] * n

        # 一進來就展開的那則（通常是最新一則），它的 OpenDocument 請求在 click() 時就打了
        states = page.evaluate(
            "() => [...document.querySelectorAll('.preview-container .pim-mailread-container[aria-expanded]')]"
            ".map(el => el.getAttribute('aria-expanded'))"
        )
        pointer = 0
        for i, st in enumerate(states):
            if st == 'true' and pointer < len(captured):
                block_unid[i] = captured[pointer]
                pointer += 1

        # 其餘一則一則展開，各自獨立等待+攔截，確保順序對得上
        for idx in range(n):
            if block_unid[idx] is not None:
                continue
            before = len(captured)
            page.evaluate("""(i) => {
                const els = [...document.querySelectorAll('.preview-container .pim-mailread-container[aria-expanded]')];
                const el = els[i];
                if (el && el.getAttribute('aria-expanded') === 'false') el.click();
            }""", idx)
            page.wait_for_timeout(900)
            new_ones = captured[before:]
            block_unid[idx] = new_ones[-1] if new_ones else None

        return block_unid, n
    finally:
        page.remove_listener("request", on_request)


def extract_message_block(page, idx):
    """抓第 idx 則訊息（`.preview-container .pim-mailread-container[aria-expanded]` 的第 idx
    個元素）自己的 sender/date/to/cc/bcc/body，跟其他訊息的內容互不干擾。"""
    return page.evaluate(r"""(i) => {
        const blocks = [...document.querySelectorAll('.preview-container .pim-mailread-container[aria-expanded]')];
        const b = blocks[i];
        if (!b) return null;
        const dateEl = b.querySelector('.pim-mailread-sentdate');
        const date = dateEl ? dateEl.innerText.split('\n').map(s => s.trim()).filter(Boolean)
            .reduce((a, x) => a.length >= x.length ? a : x, '') : '';
        const senderEls = [...b.querySelectorAll('.socpimMailSender')];
        let from = '';
        for (const el of senderEls) {
            const t = el.innerText.trim();
            if (t && t !== 'Sent by:' && t.includes('<')) { from = t; break; }
        }
        if (!from) {
            for (const el of senderEls) {
                const t = el.innerText.trim();
                if (t && t !== 'Sent by:') { from = t; break; }
            }
        }
        function parseFromTo(raw) {
            const toM  = raw.match(/To:\s*([\s\S]*?)(?:Cc:|Bcc:|Show less|$)/);
            const ccM  = raw.match(/Cc:\s*([\s\S]*?)(?:Bcc:|Show less|$)/);
            const bccM = raw.match(/Bcc:\s*([\s\S]*?)(?:Show less|$)/);
            return {
                to:  toM  ? toM[1].trim().replace(/\s+/g, ' ')  : '',
                cc:  ccM  ? ccM[1].trim().replace(/\s+/g, ' ')  : '',
                bcc: bccM ? bccM[1].trim().replace(/\s+/g, ' ') : '',
            };
        }
        const recipEl = b.querySelector('.pim-mailread-recipient');
        const toccEl  = b.querySelector('.pimToccbcc');
        const rawRecip = recipEl ? recipEl.innerText : '';
        const rawTocc  = toccEl  ? toccEl.innerText  : '';
        const rawSrc = rawRecip.includes('To:') ? rawRecip
                     : rawTocc.includes('To:')  ? rawTocc : '';
        let to = '', cc = '', bcc = '';
        if (rawSrc) {
            const parsed = parseFromTo(rawSrc);
            to = parsed.to; cc = parsed.cc; bcc = parsed.bcc;
        }
        const body = b.innerText || '';

        // 每個收件人姓名的 socpimNameBtn 都帶 socpimnameemail 屬性——有 @ 的就是
        // email，沒有的是 Domino canonical name（像 Chun-Hua Huang/elfc1/everlight
        // 這種分廠/子公司英文命名慣例）。抓成 {顯示名: canonical} 給
        // resolve_recipients() 對照 Verse 自動觸發的姓名驗證回應用，藏在 recip/tocc
        // 兩個容器裡都要抓，不分 to/cc（顯示名衝突機率低，先用簡化版）。
        const nameCanonicals = {};
        [recipEl, toccEl].forEach(el => {
            if (!el) return;
            el.querySelectorAll('.socpimNameBtn').forEach(btn => {
                const t = btn.textContent.trim();
                const c = btn.getAttribute('socpimnameemail');
                if (t && c) nameCanonicals[t] = c;
            });
        });

        return { from, to, cc, bcc, date, body, name_canonicals: nameCanonicals };
    }""", idx)


# ── 附件下載 / EML 打包（沿用 export 邏輯，整串完整存檔，不截斷）────────────────
def get_attachment_links(page):
    return page.evaluate("""() => {
        return [...document.querySelectorAll('.preview-container a[href]')]
            .filter(a => a.href.includes('$File') && a.href.includes('OpenElement'))
            .map(a => ({ name: a.innerText.trim(), href: a.href }));
    }""")


def download_attachments(links, cookies):
    session = requests.Session()
    session.cookies.update(cookies)
    attachments, seen = [], set()
    for att in links:
        if att['href'] in seen:
            continue
        seen.add(att['href'])
        # 優先從 URL 的 FileName= 取真檔名——比 a.innerText 可靠。Verse 常把 innerText
        # 渲染成籠統的操作文字（例如 "Download file"），不是真檔名，且不限於空字串或
        # 裸副檔名這兩種好偵測的形式，乾脆固定信任 URL，innerText 只當備援。
        name = None
        try:
            name = urllib.parse.unquote(att['href'].split('FileName=')[1].split('&')[0])
        except Exception:
            pass
        if not name:
            nm = (att['name'] or '').strip()
            name = urllib.parse.unquote(nm) if nm else None
        name = name or "attachment"
        resp = session.get(att['href'], verify=False, timeout=120)
        if resp.status_code == 200:
            attachments.append((name, resp.content))
    return attachments


def safe_attachment_filename(name):
    """磁碟檔名安全化：只擋 Windows 檔名不合法字元，不像 safe_filename() 那樣把非英數字
    全部替換掉——附件檔名通常已經是乾淨的原始檔名，不需要那麼激進。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().strip('.')
    return (cleaned or "attachment")[:150]


def save_attachments(attachments, unid):
    """把下載到的附件另存一份到 ATTACHMENTS_DIR（跟 .eml 分開存），檔名前綴 unid 避免
    同名衝突。回傳給 Qdrant payload 用的 [{"name": 原始檔名, "path": 存檔路徑}, ...]。"""
    saved = []
    for name, data in attachments:
        fname = f"{unid}_{safe_attachment_filename(name)}"
        path = os.path.join(ATTACHMENTS_DIR, fname)
        with open(path, 'wb') as f:
            f.write(data)
        saved.append({"name": name, "path": path})
    return saved


def _sent_date_to_rfc2822(sent_date):
    """把 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD' 轉成 RFC 2822 格式；失敗回傳空字串。"""
    if not sent_date:
        return ''
    try:
        fmt = "%Y-%m-%d %H:%M" if len(sent_date) > 10 else "%Y-%m-%d"
        dt = datetime.strptime(sent_date, fmt)
        return formatdate(dt.timestamp(), localtime=True)
    except Exception:
        return ''


def make_message_id(unid):
    """用 UNID 組出 RFC 5322 的 Message-ID（每則訊息自己的識別碼，跟回覆關聯無關）。"""
    return f"<{unid}@verse.ecic.com.tw>"


def pack_eml(meta, body, attachments, unid=None):
    msg = EmailMessage(policy=email.policy.SMTP)
    msg['From']     = meta.get('from') or meta.get('sender', '')
    msg['To']       = meta.get('to') or USERNAME
    if meta.get('cc'):
        msg['Cc'] = meta['cc']
    msg['Subject']  = meta['subject']
    msg['Date']     = _sent_date_to_rfc2822(meta.get('sent_date', '')) or meta.get('date', '')
    msg['X-Source'] = 'HCL Verse / 04Done'
    if unid:
        msg['Message-ID'] = make_message_id(unid)
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    return msg.as_bytes()


def safe_filename(subject):
    return re.sub(r'[^\w一-鿿\-_]', '_', subject)[:60]


# ── 未在通訊錄的聯絡人：Google Chat 通知（重用 hcl_write_hindsight.py --notify-only）──
def notify_new_contacts_via_chat(state_before, state_after, xlsx_path, space=NOTIFY_SPACE):
    """
    比較這次跑完後有哪些聯絡人是新增或有更新（次數/顯示名變動），組一段摘要文字，
    呼叫既有的 hcl_write_hindsight.py --notify-only 機制發 Google Chat 通知。
    不重新設計通知管道——沿用簽核通知已經接好的 n8n webhook。
    沒有新增/更新時回傳 None（不發通知）。
    """
    changed = []
    for email, info in state_after.items():
        if info.get("confirmed"):
            continue
        old = state_before.get(email)
        if old is None:
            changed.append((email, info, "新"))
        elif (set(info.get("seen_names", [])) != set(old.get("seen_names", []))
              or info.get("count") != old.get("count")):
            changed.append((email, info, "更新"))

    if not changed:
        return None

    lines = [f"📋 Verse 歸檔發現 {len(changed)} 位未在通訊錄的聯絡人待確認姓名：", ""]
    for email, info, tag in changed:
        names = "、".join(info.get("seen_names", []))
        lines.append(f"- [{tag}] {email}（{names}，共 {info.get('count')} 次）")
    lines.append("")
    lines.append(f"請開啟 {xlsx_path} 填寫 canonical_name 欄位，填完跟我說一聲。")
    text = "\n".join(lines)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(text)
        result = subprocess.run(
            [sys.executable, NOTIFY_SCRIPT, "--notify-only",
             "--notify-file", tmp_path, "--space", space],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return result.returncode == 0
    finally:
        os.unlink(tmp_path)


# ── 移到 domdom（沿用 move_construction v1.2.2 機制）──────────────────────────
def move_to_folder(page, folder=TARGET_FOLDER):
    """假設目標信件已被選取（reading pane 已開）。點資料夾 icon → 輸入名稱 → 選取。"""
    # 直接鎖定每封信的「Move to folder」鈕本身（class 固定），不依賴父層 action-tray-populated
    # （Inbox 檢視父層有 .action-tray-populated，資料夾檢視沒有 → 不能用父層比對）
    MOVE_BTN_SEL = "button.action.pim-move-to-folder.icon"
    try:
        page.wait_for_selector(MOVE_BTN_SEL, timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(300)

    # 取可見的那一個（排除隱藏的同名 button）
    move_btn = None
    btns = page.locator(MOVE_BTN_SEL)
    for i in range(btns.count()):
        if btns.nth(i).is_visible():
            move_btn = btns.nth(i)
            break
    if move_btn is None:
        return "error_no_button"
    move_btn.click()
    page.wait_for_timeout(500)

    try:
        page.wait_for_selector("div.folder-tray-float.show", timeout=8000)
    except Exception:
        return "error_no_popup"

    # Verse 自己的資料夾搜尋框對含括號的名稱完全比對不到（實測：打完整
    # 「已上傳Gmail(暫時找信)」回傳 0 筆，但打去掉括號後綴的「已上傳Gmail」能
    # 正確篩到剩這一個資料夾）——搜尋只用去掉結尾括號註記的版本，實際點擊仍用
    # 完整 folder 名稱做 has-text 比對，確保點到的是名稱完全相符的那個
    search_term = re.sub(r'[（(][^）)]*[）)]\s*$', '', folder).strip() or folder
    folder_input = page.locator("div.folder-tray-float.show input.folder-search-input")
    folder_input.click()
    folder_input.fill("")
    folder_input.type(search_term, delay=50)
    page.wait_for_timeout(1000)

    folder_item = page.locator(
        f"div.folder-tray-float.show [role='treeitem']:visible:has-text('{folder}')"
    ).first
    try:
        folder_item.wait_for(state="visible", timeout=5000)
        folder_item.click()
        page.wait_for_timeout(1500)
    except Exception:
        folder_input.press("Enter")
        page.wait_for_timeout(1500)

    if page.locator("div.folder-tray-float.show").count() > 0:
        try:
            folder_input.press("Enter")
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if page.locator("div.folder-tray-float.show").count() > 0:
            return "error_popup_stuck"
    return "moved"


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main():
    ensure_collection()
    hindsight = HindsightClient(HINDSIGHT_URL)
    results = []
    seen_rows = set()
    # 有些信件（實測案例：Confidential/秘密 機密信件）Verse 本身就會停用「移動到
    # 資料夾」這個動作（不是自動化的 bug，等過 30 秒讓畫面完全載入、翻過 More
    # actions 選單都確認過真的沒有這個選項），移動一定會失敗。這種信只略過、
    # 記進這個集合，之後選列時跳過，不要讓它擋住後面所有信的處理進度。
    skip_row_sigs = set()

    # 未在 email_mapping 查到的聯絡人（外部廠商/離職同仁）追蹤用；deep copy 一份
    # 起始快照，跑完後拿來比對這次有沒有新增/更新，決定要不要重新產生 Excel + 通知
    contacts_state_before = load_contacts_state()
    contacts_state = json.loads(json.dumps(contacts_state_before))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADFUL, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True, locale="en-US")
        page = ctx.new_page()
        page.set_default_timeout(60000)

        # Verse 自己不時會觸發姓名驗證 API（s_ValidationJson，不一定跟我們正在處理的
        # 這封信有關，folder 列表渲染其他信件的名字也會觸發）——被動撿現成的回應累積
        # 成 name_directory（見 parse_validation_response() 說明），同時從請求本身的
        # header 撿 X-IBM-INotes-Nonce（頁面上找不到固定來源，只能這樣被動取得）。
        # 一旦有 nonce，之後每則訊息就能用 resolve_canonical_names_via_api() 主動幫
        # 沒解析到的收件人補送一次批次請求，不用一個一個點名片卡。
        name_directory = {}
        session_nonce = {"value": None}

        def _on_response(resp):
            if "s_ValidationJson" in resp.url:
                try:
                    name_directory.update(parse_validation_response(resp.text()))
                except Exception:
                    pass

        def _on_request(req):
            if "s_ValidationJson" in req.url:
                nonce = req.headers.get("x-ibm-inotes-nonce")
                if nonce:
                    session_nonce["value"] = nonce

        page.on("response", _on_response)
        page.on("request", _on_request)

        try:
            print("登入 HCL Verse...")
            login(page)
            if PROJ_TAG:
                print(f"（本次會額外標記 Hindsight tag：proj:{PROJ_TAG}）")
            print(f"開啟資料夾「{SOURCE_FOLDER}」...")
            open_folder(page, SOURCE_FOLDER)
            ensure_thread_grouping_off(page)

            count = page.locator('.seq-msg-row').count()
            limit_unit = "則訊息" if BY_MESSAGES else "封"
            print(f"「{SOURCE_FOLDER}」目前可見 {count} 封，開始處理（上限 {MAX_RESULTS}{limit_unit}"
                  f"{'，--no-move 不移動' if NO_MOVE else ''}）...\n")

            processed = 0
            msg_running_total = 0
            while (msg_running_total if BY_MESSAGES else processed) < MAX_RESULTS:
                rows = page.locator('.seq-msg-row')
                if rows.count() == 0:
                    print("資料夾已清空，結束。")
                    break

                # 依序找第一封「不在略過名單裡」的信——不能盲用 rows.first，否則
                # 一封移不動的信（見 skip_row_sigs 註解）會永遠卡在最上面，擋住
                # 後面所有信的處理
                total_rows = rows.count()
                item = None
                meta = None
                for idx in range(total_rows):
                    candidate = rows.nth(idx)
                    cand_meta = None
                    for _ in range(6):  # 該列可能還在渲染，重試
                        cand_meta = parse_msg_row(candidate)
                        if cand_meta:
                            break
                        page.wait_for_timeout(600)
                        candidate = page.locator('.seq-msg-row').nth(idx)
                    if not cand_meta:
                        continue
                    cand_sig = make_row_signature(cand_meta["subject"], cand_meta["sender"], cand_meta["snippet"])
                    if cand_sig in skip_row_sigs:
                        continue
                    item, meta = candidate, cand_meta
                    break
                if not meta:
                    print("  可見信件都已略過或無法解析，結束。")
                    break

                # 安全閥：若這一列跟上一輪處理過的一樣（代表移動失敗它還在頂部）→ 停止
                row_sig = make_row_signature(meta["subject"], meta["sender"], meta["snippet"])
                if row_sig in seen_rows:
                    print(f"  ⚠️ 偵測到重複信件（移動可能失敗），停止：{meta['subject'][:40]}")
                    break
                seen_rows.add(row_sig)

                # 點開 + 展開全部訊息 + 攔截每則的 Domino UNID
                block_unids, n_blocks = open_row_and_get_block_unids(page, item)

                # 整串（thread 級）header/raw，給 EML 完整存檔用（不截斷）
                thread_header = extract_header_fields(page)
                thread_raw    = page.locator('.preview-container').inner_text().strip()
                thread_sender_email, thread_sender_name, thread_sender_found = resolve_sender(
                    thread_header.get("from") or meta["sender"])
                thread_date_str  = thread_header.get("date") or ""
                thread_sent_date = normalize_sent_date(thread_date_str)
                if thread_sender_email and not thread_sender_found:
                    track_unknown_contact(thread_sender_email, thread_sender_name,
                                           thread_sent_date, contacts_state)
                # 逐則訊息：各自 clean+砍引用歷史，各自獨立 RAG+Hindsight
                messages = []
                for idx in range(n_blocks):
                    blk = extract_message_block(page, idx)
                    if not blk:
                        continue
                    if not blk.get("date") and not blk.get("from"):
                        # Verse 自己判定這則已被後面訊息的引用完整涵蓋，只給精簡摘要
                        # （沒有完整表頭可抓）——不用重複處理
                        continue
                    body_clean = clean_body(blk["body"], meta["subject"])
                    if len(body_clean) < 3:
                        continue
                    sender_email, sender_name, sender_found = resolve_sender(blk.get("from"))
                    sent_date_for_block = normalize_sent_date(blk.get("date", ""))
                    if sender_email and not sender_found:
                        track_unknown_contact(sender_email, sender_name,
                                               sent_date_for_block, contacts_state)
                    unid = block_unids[idx] or make_id(
                        meta["subject"], sender_email or sender_name, blk.get("date", ""))

                    # 這則訊息裡沒有 email、只有 Domino canonical name 的收件人
                    # （分廠/子公司英文命名慣例註冊的帳號），且還沒在 name_directory
                    # 裡的，主動補送一次批次驗證請求解析成中文名——不用一個一個點
                    # 名片卡。第一封信最開頭可能還沒攔到任何 nonce，這種情況就先跳過，
                    # 該次留英文顯示名，下一封信/下一則訊息通常就有 nonce 了
                    try:
                        resolve_unresolved_canonicals(
                            page, blk.get("name_canonicals"), name_directory, session_nonce["value"])
                    except Exception as e:
                        print(f"  ⚠️ 收件人姓名批次驗證失敗（不影響信件本身寫入）：{e}")

                    messages.append({
                        "unid": unid,
                        "sender_email": sender_email,
                        "sender_name": sender_name,
                        # RAG/Hindsight 只需要可讀的姓名，不需要 email——EML 那邊另外用
                        # thread_header/thread_raw 的原始值（含 email、不砍引用），兩邊互不影響。
                        # name_canonicals/name_directory 讓沒有 <email> 的收件人（分廠/子公司
                        # 英文 canonical name 註冊的帳號）也能轉中文，見 resolve_recipients() 說明
                        "to": resolve_recipients(substitute_me(blk.get("to", "")),
                                                  blk.get("name_canonicals"), name_directory,
                                                  contacts_state, sent_date_for_block),
                        "cc": resolve_recipients(substitute_me(blk.get("cc", "")),
                                                  blk.get("name_canonicals"), name_directory,
                                                  contacts_state, sent_date_for_block),
                        # 給 EML 用，保留 email；quote_recipient_header() 把「Lastname,
                        # Firstname」這種名字裡帶逗號的顯示名加上雙引號，避免 Gmail
                        # 解析 To:/Cc: 信頭時把逗號誤判成收件人分隔符、拆成兩個人
                        "to_raw": quote_recipient_header(substitute_me(blk.get("to", ""))),
                        "cc_raw": quote_recipient_header(substitute_me(blk.get("cc", ""))),
                        "date": blk.get("date", ""),
                        "sent_date": normalize_sent_date(blk.get("date", "")),
                        "body": body_clean,
                        # 給 EML 用：只剝 Verse 自己的 UI chrome，不砍引用歷史（引用是原始信件
                        # 內容的一部分，EML 要保留信件原貌，不能動）
                        "eml_body": _strip_ui_noise(blk["body"], meta["subject"]),
                    })

                if not messages:
                    # 保底：一則都沒抓到就退回整串當一則處理，避免整封信被跳過
                    try:
                        resolve_unresolved_canonicals(
                            page, thread_header.get("name_canonicals"), name_directory,
                            session_nonce["value"])
                    except Exception as e:
                        print(f"  ⚠️ 收件人姓名批次驗證失敗（不影響信件本身寫入）：{e}")
                    messages = [{
                        "unid": make_id(meta["subject"], thread_sender_email, thread_date_str),
                        "sender_email": thread_sender_email,
                        "sender_name": thread_sender_name,
                        "to": resolve_recipients(thread_header.get("to", ""),
                                                  thread_header.get("name_canonicals"), name_directory,
                                                  contacts_state, thread_sent_date),
                        "cc": resolve_recipients(thread_header.get("cc", ""),
                                                  thread_header.get("name_canonicals"), name_directory,
                                                  contacts_state, thread_sent_date),
                        "to_raw": quote_recipient_header(thread_header.get("to", "")),
                        "cc_raw": quote_recipient_header(thread_header.get("cc", "")),
                        "date": thread_date_str,
                        "sent_date": thread_sent_date,
                        "body": clean_body(thread_raw, meta["subject"]),
                        "eml_body": _strip_ui_noise(thread_raw, meta["subject"]),
                    }]

                rec = {"subject": meta["subject"], "from": thread_sender_name,
                       "date": thread_date_str, "sent_date": thread_sent_date,
                       "message_count": len(messages)}

                # 下載每則訊息自己的附件、另存一份到 ATTACHMENTS_DIR（跟 .eml 分開存，
                # 檔名前綴 unid 避免同名衝突）。要在 RAG 那步之前做，才能把存檔位置寫進
                # Qdrant payload；同一份 bytes 留給下面 EML 打包用，不用重複下載。
                att_links = get_attachment_links(page)
                cookies   = {c['name']: c['value'] for c in page.context.cookies()}
                for m in messages:
                    own_links = [a for a in att_links if m["unid"] and m["unid"] in a['href']]
                    attachments_data = download_attachments(own_links, cookies) if own_links else []
                    m["_attachment_data"] = attachments_data
                    m["attachments"] = save_attachments(attachments_data, m["unid"])

                    # 會議記錄/報價單附件 -> 只先另存到 MEETING_QUOTE_STAGING_DIR，不在歸檔
                    # 當下同步跑 RAGAnything（單一附件解析可能要跑好幾分鐘，會拖慢整支
                    # pipeline）。事後另外執行 meeting_quote_batch_process.py 批次處理。
                    # 只認 .pdf、檔名或主旨符合關鍵字的附件；失敗只印警告，不中斷這封信原本的
                    # RAG/Hindsight/EML/搬移流程。
                    try:
                        mq_records = process_meeting_quote_attachments(
                            m["unid"], meta["subject"], m["sender_name"],
                            m["sent_date"], attachments_data, body=m.get("body", ""))
                        for r in mq_records:
                            flag = "✓" if r["saved"] else "✗"
                            print(f"    {flag} 會議記錄/報價單附件[{','.join(r['labels'])}] {r['name'][:40]}"
                                  f" -> 已存檔待批次處理" + (f"（{r.get('error','')[:120]}）" if r.get("error") else ""))
                    except Exception as e:
                        print(f"  ⚠️ 會議記錄/報價單附件處理失敗（不影響信件本身寫入）：{e}")

                # ① RAG 索引 + ③ Hindsight retain（逐則訊息各自一筆）
                rag_ok = hindsight_ok = skipped_dup = 0
                for m in messages:
                    if already_indexed(m["unid"]):
                        skipped_dup += 1
                        print(f"  ↷ 已存在（unid={m['unid']}），跳過不寫入：{m['sender_name']}")
                        continue
                    try:
                        text = f"{meta['subject']} {m['body']}"
                        embedding = get_embedding(text)
                        _fresh_qdrant().upsert(collection_name=COLLECTION, points=[PointStruct(
                            id=id_to_uuid(m["unid"]),
                            vector=embedding,
                            payload={
                                "subject": meta["subject"], "body": m["body"],
                                "from_email": m["sender_email"], "from_name": m["sender_name"],
                                "to": m["to"], "cc": m["cc"],
                                "date": m["date"], "sent_date": m["sent_date"],
                                "unid": m["unid"],
                                "attachments": m.get("attachments", []),
                            })])
                        rag_ok += 1
                    except Exception as e:
                        print(f"  ✗ RAG 失敗（{m['sender_name']}）：{e}")

                    try:
                        metadata = {
                            "subject":    meta["subject"],
                            "from_email": m["sender_email"],
                            "from_name":  m["sender_name"],
                            "to":         m["to"],
                            "unid":       m["unid"],
                            "sent_date":  m["sent_date"],
                        }
                        result = hindsight.retain(
                            content=(
                                f"主旨：{meta['subject']}\n"
                                f"寄件者：{m['sender_name']}"
                                + (f" <{m['sender_email']}>" if m['sender_email'] else "")
                                + f"\n日期：{m['sent_date']}\n\n{m['body']}"
                            ),
                            document_id=m["unid"],
                            timestamp=m["sent_date"],
                            metadata=metadata,
                            # 重新加回 tags——這次不是為了 proj 分類（那個還是暫緩），
                            # 是為了讓 reflect()/recall() 能用 tags=["mail"] 過濾，
                            # 避免跟同一個 EID bank 裡其他 skill 寫入的資料（例如
                            # hcl-notes-approval 的簽核記錄）混在一起污染查詢結果
                            tags=(["mail", f"proj:{PROJ_TAG}"] if PROJ_TAG else ["mail"]),
                            context=f"HCL Verse 信件：主旨「{meta['subject']}」，寄件者 {m['sender_name']}",
                        )
                        result_text = result.get("result", {}).get("content", [{}])[0].get("text", "")
                        if "validation error" in result_text.lower():
                            print(f"  ✗ Hindsight 被拒絕（{m['sender_name']}）：{result_text[:200]}")
                        else:
                            hindsight_ok += 1
                    except Exception as e:
                        print(f"  ✗ Hindsight 失敗（{m['sender_name']}）：{e}")

                rec["rag_ok"] = rag_ok
                rec["hindsight_ok"] = hindsight_ok
                rec["skipped_dup"] = skipped_dup

                # ② EML 匯出：每則訊息各自一個 .eml（跟 RAG/Hindsight 那份訊息級+去重的
                # 資料是分開的兩種用途）。內文只剝 Verse 的 UI chrome，不砍引用歷史——
                # 引用是原始信件內容的一部分，EML 要保留信件原貌讓人回溯查閱、上傳 Gmail。
                # 只帶 Message-ID（每則自己的識別碼），不組 In-Reply-To/References——
                # 每則訊息在 Gmail 都是獨立的信，不自動合併討論串（見檔案開頭說明）。
                try:
                    eml_paths, all_attachment_names = [], []
                    for i, m in enumerate(messages):
                        attachments = m.get("_attachment_data", [])
                        eml_meta = {
                            "from": m["sender_email"] or m["sender_name"],
                            "to": m.get("to_raw", m["to"]), "cc": m.get("cc_raw", m["cc"]),
                            "subject": meta["subject"], "date": m["date"],
                            "sent_date": m["sent_date"],
                        }
                        eml_bytes = pack_eml(eml_meta, m["eml_body"], attachments, unid=m["unid"])
                        # 檔名就用 unid：主旨/寄件者組出來的檔名不好查詢，
                        # unid 本身就是唯一、可回頭比對 Qdrant/Hindsight 的 key
                        fname = f"{m['unid']}.eml"
                        eml_path = os.path.join(EML_UNDO_DIR, fname)
                        with open(eml_path, 'wb') as f:
                            f.write(eml_bytes)
                        eml_paths.append(eml_path)
                        all_attachment_names.extend(a[0] for a in attachments)
                    rec["eml"] = eml_paths
                    rec["attachments"] = all_attachment_names
                except Exception as e:
                    rec["eml"] = f"fail: {e}"
                    print(f"  ✗ EML 失敗：{e}")

                # ④ 移到 domdom
                if NO_MOVE:
                    rec["move"] = "skipped (--no-move)"
                    print(f"[{processed+1}] ✓ {meta['subject'][:40]} "
                          f"({len(messages)} 則訊息, RAG {rag_ok}/{len(messages)}, "
                          f"Hindsight {hindsight_ok}/{len(messages)}, 附件{len(rec.get('attachments', []))}, 不移動)")
                    results.append(rec)
                    processed += 1
                    msg_running_total += len(messages)
                    # --no-move 模式無法移出，否則會重複處理同一封 → 只處理第一封後停
                    print("  （--no-move 模式：只處理目前頂部第一封以驗證流程，結束。）")
                    break
                else:
                    status = move_to_folder(page, TARGET_FOLDER)
                    rec["move"] = status
                    flag = "✓" if status == "moved" else "✗"
                    print(f"[{processed+1}] {flag} {meta['subject'][:40]} "
                          f"({len(messages)} 則訊息, RAG {rag_ok}/{len(messages)}, "
                          f"Hindsight {hindsight_ok}/{len(messages)}, move={status})")
                    if status != "moved":
                        results.append(rec)
                        # RAG/Hindsight 這封已經寫完了，只是移不動（實測過：Verse
                        # 對某些信件——例如機密信——本身就會停用移動這個動作，不是
                        # 自動化的 bug，重試也不會好）。記進略過名單，繼續處理下一
                        # 封，不要讓這一封擋住整批進度；這封留在原資料夾，之後人工
                        # 處理
                        skip_row_sigs.add(row_sig)
                        print(f"  ↷ 移動失敗，略過這封（留在原資料夾），繼續下一封：{meta['subject'][:40]}")
                        continue
                    page.wait_for_timeout(1500)  # 等清單刷新

                results.append(rec)
                processed += 1
                msg_running_total += len(messages)

        finally:
            browser.close()

    moved        = sum(1 for r in results if r.get("move") == "moved")
    rag_ok_total       = sum(r.get("rag_ok", 0) for r in results)
    hindsight_ok_total = sum(r.get("hindsight_ok", 0) for r in results)
    message_total      = sum(r.get("message_count", 0) for r in results)
    sent_dates = sorted(d[:10] for d in (r.get("sent_date") or "" for r in results) if d)
    summary = {
        "source": SOURCE_FOLDER, "target": TARGET_FOLDER,
        "no_move": NO_MOVE, "output_dir": OUTPUT_DIR,
        "archived_date": datetime.now().strftime("%Y-%m-%d"),  # 歸檔（寫入）日
        "sent_date_range": (
            {"earliest": sent_dates[0], "latest": sent_dates[-1]} if sent_dates else None
        ),  # 本批信件實際寄件日範圍
        "processed": len(results), "message_total": message_total,
        "rag_ok": rag_ok_total, "hindsight_ok": hindsight_ok_total, "moved": moved,
        "emails": results,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 完成：處理 {len(results)} 封（共 {message_total} 則訊息），"
          f"RAG {rag_ok_total}/{message_total} / Hindsight {hindsight_ok_total}/{message_total} 成功，"
          f"移動 {moved} 封 → {TARGET_FOLDER}")
    print(f"  結果已寫入 {OUTPUT_FILE}")

    # 未在 email_mapping 查到的聯絡人：有新增/更新才存檔 + 重新產生 Excel + 通知 Google Chat
    if contacts_state != contacts_state_before:
        save_contacts_state(contacts_state)
    if contacts_have_new_or_updated(contacts_state_before, contacts_state):
        n_pending = generate_contacts_excel(contacts_state, EXTERNAL_CONTACTS_XLSX)
        print(f"  外部聯絡人待確認清單更新：{n_pending} 位，已寫入 {EXTERNAL_CONTACTS_XLSX}")
        try:
            notified = notify_new_contacts_via_chat(
                contacts_state_before, contacts_state, EXTERNAL_CONTACTS_XLSX)
            print(f"  Google Chat 通知：{'已發送' if notified else '略過（無變動）'}")
        except Exception as e:
            print(f"  ✗ Google Chat 通知失敗：{e}")


if __name__ == "__main__":
    main()
