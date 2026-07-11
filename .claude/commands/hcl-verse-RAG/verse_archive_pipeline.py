#!/usr/bin/env python3
"""
HCL Verse 歸檔 pipeline
======================
從「04Done」資料夾逐封處理：抓全文+附件 → 建 RAG 索引(Qdrant) + 存成 .eml
→ 處理完移到「domdom」資料夾（移出來源 = 天然去重游標）。

兩種用途、兩份不同粒度的資料，互不影響：
- RAG(Qdrant)/Hindsight：討論串（thread）拆成「訊息級」處理，每則訊息各自用 Domino
  UNID 當 document_id（不是 hash 畫面文字），各自獨立一筆；每則的引用歷史
  （quote-in-body）會被截斷，避免重複內容灌爆 Hindsight；同時比對被砍掉的引用身份
  資訊，配對出 reply_to_unid 存進 payload/metadata，記錄訊息間的回覆關聯
- EML：整封信/整串完整存檔（不截斷、不砍引用），保留信件原貌，供人工回溯查閱、
  上傳 Gmail 用

用法：
    python3 verse_archive_pipeline.py [max_results] [--no-move] [--headful]

    max_results   處理上限，預設 50
    --no-move     只做 EML+RAG，不移動信件（測試用，不會動到信箱）
    --headful     顯示瀏覽器視窗（除錯用；預設 headless）
"""
import os, tempfile, sys, re, json, hashlib, warnings, urllib.parse, subprocess
from datetime import datetime, timedelta
import requests
from email.message import EmailMessage
import email.policy
from email.utils import formatdate
warnings.filterwarnings('ignore')  # 關閉內部 SSL 憑證警告

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
from quote_stripper import strip_quoted_history, strip_quoted_history_with_identity
from email_mapping import email_to_name, resolve_me
from external_contacts_tracker import (
    load_state as load_contacts_state,
    save_state as save_contacts_state,
    track_unknown_contact,
    has_new_or_updated as contacts_have_new_or_updated,
)
from external_contacts_excel import generate_excel as generate_contacts_excel

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")


class HindsightClient:
    def __init__(self, url):
        self.url = url
        resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "verse-archive", "version": "1.0"}},
        })
        self.session_id = resp.headers.get("mcp-session-id")

    def retain(self, content, document_id, timestamp, tags, metadata, context):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "retain", "arguments": {
                "content": content, "document_id": document_id,
                "timestamp": timestamp, "tags": tags,
                "metadata": metadata, "context": context,
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

SOURCE_FOLDER = "04Done"
TARGET_FOLDER = "domdom"
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
OUTPUT_DIR  = os.path.expanduser("~/verse-export")
os.makedirs(OUTPUT_DIR, exist_ok=True)
EXTERNAL_CONTACTS_XLSX = os.path.join(OUTPUT_DIR, "external_contacts.xlsx")
# 附件另存一份（跟 .eml 分開），全部平放同一個資料夾，檔名前綴 unid 避免同名衝突
ATTACHMENTS_DIR = os.path.join(OUTPUT_DIR, "attachments")
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

qdrant        = QdrantClient(url=QDRANT_URL)
openai_client = OpenAI(api_key=OPENAI_KEY or "local-no-key-needed", base_url=EMBEDDING_API_BASE)


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


def make_row_signature(subject, sender, snippet):
    """安全閥用的『這一列信件』簽章，跟訊息級 id 無關。"""
    return hashlib.md5(f"{sender}|{subject}|{snippet}".encode()).hexdigest()


def make_thread_id(subject):
    normalized = re.sub(
        r'^(回覆[:：]\s*|RE[:：]\s*|FW[:：]\s*|Fwd[:：]\s*)+',
        '', subject, flags=re.IGNORECASE
    ).strip()
    return hashlib.md5(normalized.encode()).hexdigest()


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


def resolve_recipients(raw):
    """把 to/cc 字串裡每個 'Name <email>' 或純 email 都換成通訊錄查到的姓名，
    只給 RAG/Hindsight 用（可讀性優先，不需要真的 email）。EML 那邊要保留原始
    收件人資訊（含 email），不要走這個函式——兩種輸出用途不同，見 pipeline 文件說明。"""
    if not raw:
        return raw
    names = []
    for part in (p.strip() for p in raw.split(',')):
        if not part:
            continue
        m = re.match(r'^"?([^"<>]*)"?\s*<([^<>]+)>$', part)
        if m:
            display, addr = m.group(1).strip(), m.group(2).strip()
            name = email_to_name(addr)
            names.append(name if name != addr else (display or addr))
        elif '@' in part:
            name = email_to_name(part)
            names.append(name if name != part else part)
        else:
            names.append(part)  # 已經是純姓名（Notes 內部位址常見），原樣保留
    return "、".join(names)


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


def open_folder(page, folder_name):
    """
    點擊左側資料夾樹中名為 folder_name 的資料夾，載入其信件清單。
    Inbox 有專屬 class `.inbox`；自訂資料夾（如 04Done）只能靠名字點，
    且可能藏在「資料夾 / Folders」摺疊群組裡，需先展開。
    """
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


def clean_body_and_identify(raw, subject):
    """跟 clean_body() 一樣，但額外回傳被砍掉那段引用歷史的身份資訊
    (quoted_sender, quoted_date)，給之後配對 reply_to_unid 用。
    回傳 (body_clean, quoted_sender, quoted_date)。"""
    return strip_quoted_history_with_identity(_strip_ui_noise(raw, subject))


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
        return { from, to, cc, bcc, date, label_ids: [...labelSet] };
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
        return { from, to, cc, bcc, date, body };
    }""", idx)


# ── 建立訊息間的回覆關聯（reply_to_unid）──────────────────────────────────────
def match_reply_to(messages):
    """幫每則訊息找出它引用/回覆的是同一個 thread 裡的哪一則，寫進 reply_to_unid。
    依據是 clean_body_and_identify() 抓到的「被引用者姓名/日期」，跟同一批訊息的
    sender_name/sender_email/sent_date 比對。找不到或無法唯一判斷就留 None，不亂猜。
    """
    for m in messages:
        quoted_sender = m.pop("_quoted_sender", None)
        quoted_date = m.pop("_quoted_date", None)
        m["reply_to_unid"] = None
        if not quoted_sender:
            continue

        qs = quoted_sender.strip()
        candidates = []
        for other in messages:
            if other is m:
                continue
            name = (other.get("sender_name") or "").strip()
            email = (other.get("sender_email") or "").strip()
            if not name and not email:
                continue
            if qs == name or (email and qs.lower() == email.lower()) or \
               (name and (qs in name or name in qs)):
                candidates.append(other)

        if len(candidates) == 1:
            m["reply_to_unid"] = candidates[0]["unid"]
        elif len(candidates) > 1 and quoted_date:
            quoted_norm = normalize_sent_date(quoted_date)
            exact = [c for c in candidates if quoted_norm and c.get("sent_date") == quoted_norm]
            if len(exact) == 1:
                m["reply_to_unid"] = exact[0]["unid"]
    return messages


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
        resp = session.get(att['href'], verify=False)
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
    """用 UNID 組出 RFC 5322 的 Message-ID，讓 mail client（含 Gmail）能靠標準信頭
    自動重建討論串關聯，不用自己另外做 UI 呈現。"""
    return f"<{unid}@verse.ecic.com.tw>"


def pack_eml(meta, body, attachments, unid=None, reply_to_unid=None):
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
    if reply_to_unid:
        parent_id = make_message_id(reply_to_unid)
        msg['In-Reply-To'] = parent_id
        msg['References'] = parent_id
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

    folder_input = page.locator("div.folder-tray-float.show input.folder-search-input")
    folder_input.click()
    folder_input.fill("")
    folder_input.type(folder, delay=50)
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

    # 未在 email_mapping 查到的聯絡人（外部廠商/離職同仁）追蹤用；deep copy 一份
    # 起始快照，跑完後拿來比對這次有沒有新增/更新，決定要不要重新產生 Excel + 通知
    contacts_state_before = load_contacts_state()
    contacts_state = json.loads(json.dumps(contacts_state_before))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADFUL, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True, locale="en-US")
        page = ctx.new_page()
        page.set_default_timeout(60000)
        try:
            print("登入 HCL Verse...")
            login(page)
            print(f"開啟資料夾「{SOURCE_FOLDER}」...")
            open_folder(page, SOURCE_FOLDER)

            count = page.locator('.seq-msg-row').count()
            print(f"「{SOURCE_FOLDER}」目前可見 {count} 封，開始處理（上限 {MAX_RESULTS}"
                  f"{'，--no-move 不移動' if NO_MOVE else ''}）...\n")

            processed = 0
            while processed < MAX_RESULTS:
                rows = page.locator('.seq-msg-row')
                if rows.count() == 0:
                    print("資料夾已清空，結束。")
                    break

                item = rows.first
                meta = None
                for _ in range(6):  # 首列可能還在渲染，重試
                    meta = parse_msg_row(item)
                    if meta:
                        break
                    page.wait_for_timeout(600)
                    item = page.locator('.seq-msg-row').first
                if not meta:
                    print("  ⚠️ 無法解析第一列，停止。")
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
                thread_id = make_thread_id(meta["subject"])

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
                    body_clean, quoted_sender, quoted_date = clean_body_and_identify(
                        blk["body"], meta["subject"])
                    if len(body_clean) < 3:
                        continue
                    sender_email, sender_name, sender_found = resolve_sender(blk.get("from"))
                    sent_date_for_block = normalize_sent_date(blk.get("date", ""))
                    if sender_email and not sender_found:
                        track_unknown_contact(sender_email, sender_name,
                                               sent_date_for_block, contacts_state)
                    unid = block_unids[idx] or make_id(
                        meta["subject"], sender_email or sender_name, blk.get("date", ""))
                    messages.append({
                        "unid": unid,
                        "sender_email": sender_email,
                        "sender_name": sender_name,
                        # RAG/Hindsight 只需要可讀的姓名，不需要 email——EML 那邊另外用
                        # thread_header/thread_raw 的原始值（含 email、不砍引用），兩邊互不影響
                        "to": resolve_recipients(substitute_me(blk.get("to", ""))),
                        "cc": resolve_recipients(substitute_me(blk.get("cc", ""))),
                        "to_raw": substitute_me(blk.get("to", "")),  # 給 EML 用，保留 email
                        "cc_raw": substitute_me(blk.get("cc", "")),
                        "date": blk.get("date", ""),
                        "sent_date": normalize_sent_date(blk.get("date", "")),
                        "body": body_clean,
                        # 給 EML 用：只剝 Verse 自己的 UI chrome，不砍引用歷史（引用是原始信件
                        # 內容的一部分，EML 要保留信件原貌，不能動）
                        "eml_body": _strip_ui_noise(blk["body"], meta["subject"]),
                        "_quoted_sender": quoted_sender,
                        "_quoted_date": quoted_date,
                    })

                match_reply_to(messages)  # 幫每則訊息配對它回覆的是同一 thread 裡的哪一則

                if not messages:
                    # 保底：一則都沒抓到就退回整串當一則處理，避免整封信被跳過
                    messages = [{
                        "unid": make_id(meta["subject"], thread_sender_email, thread_date_str),
                        "sender_email": thread_sender_email,
                        "sender_name": thread_sender_name,
                        "to": resolve_recipients(thread_header.get("to", "")),
                        "cc": resolve_recipients(thread_header.get("cc", "")),
                        "to_raw": thread_header.get("to", ""),
                        "cc_raw": thread_header.get("cc", ""),
                        "date": thread_date_str,
                        "sent_date": thread_sent_date,
                        "body": clean_body(thread_raw, meta["subject"]),
                        "eml_body": _strip_ui_noise(thread_raw, meta["subject"]),
                        "reply_to_unid": None,
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

                # ① RAG 索引 + ③ Hindsight retain（逐則訊息各自一筆）
                rag_ok = hindsight_ok = 0
                for m in messages:
                    try:
                        text = f"{meta['subject']} {m['body']}"
                        embedding = get_embedding(text)
                        qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
                            id=id_to_uuid(m["unid"]),
                            vector=embedding,
                            payload={
                                "subject": meta["subject"], "body": m["body"],
                                "from_email": m["sender_email"], "from_name": m["sender_name"],
                                "to": m["to"], "cc": m["cc"],
                                "date": m["date"], "sent_date": m["sent_date"],
                                "thread_id": thread_id, "unid": m["unid"],
                                "reply_to_unid": m.get("reply_to_unid"),
                                "attachments": m.get("attachments", []),
                            })])
                        rag_ok += 1
                    except Exception as e:
                        print(f"  ✗ RAG 失敗（{m['sender_name']}）：{e}")

                    try:
                        tags = ["source:verse"]  # proj tag 先不分類，事後用同一個 document_id 補上（覆蓋 tags）
                        metadata = {
                            "subject":    meta["subject"],
                            "from_email": m["sender_email"],
                            "from_name":  m["sender_name"],
                            "to":         m["to"],
                            "cc":         m["cc"],
                            "thread_id":  thread_id,
                            "unid":       m["unid"],
                            "sent_date":  m["sent_date"],
                        }
                        # Hindsight schema 要求 metadata 值是字串——reply_to_unid 是 None
                        # （原始信，沒有回覆對象）時整個省略這個 key，傳 null 會被 validation 擋下來
                        if m.get("reply_to_unid"):
                            metadata["reply_to_unid"] = m["reply_to_unid"]
                        result = hindsight.retain(
                            content=(
                                f"主旨：{meta['subject']}\n"
                                f"寄件者：{m['sender_name']}"
                                + (f" <{m['sender_email']}>" if m['sender_email'] else "")
                                + f"\n日期：{m['sent_date']}\n\n{m['body']}"
                            ),
                            document_id=m["unid"],
                            timestamp=m["sent_date"],
                            tags=tags,
                            metadata=metadata,
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

                # ② EML 匯出：每則訊息各自一個 .eml（跟 RAG/Hindsight 那份訊息級+去重的
                # 資料是分開的兩種用途）。內文只剝 Verse 的 UI chrome，不砍引用歷史——
                # 引用是原始信件內容的一部分，EML 要保留信件原貌讓人回溯查閱、上傳 Gmail。
                # 帶 Message-ID/In-Reply-To（用 unid/reply_to_unid 組），Gmail 收到後會照
                # 這些標準信頭自動重建討論串關聯。
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
                        eml_bytes = pack_eml(eml_meta, m["eml_body"], attachments,
                                             unid=m["unid"], reply_to_unid=m.get("reply_to_unid"))
                        fname = f"{safe_filename(meta['subject'])}_{i:02d}_{safe_filename(m['sender_name'])}.eml"
                        eml_path = os.path.join(OUTPUT_DIR, fname)
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
                        print("  ✗ 移動失敗，停止以免重複處理。")
                        break
                    page.wait_for_timeout(1500)  # 等清單刷新

                results.append(rec)
                processed += 1

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
