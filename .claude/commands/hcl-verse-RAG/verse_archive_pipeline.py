#!/usr/bin/env python3
"""
HCL Verse 甇豢? pipeline
======================
敺?4Done???冗????嚗??冽?+?辣 ??撱?RAG 蝝Ｗ?(Qdrant) + 摮? .eml
????摰宏?啜omdom???冗嚗宏?箔?皞?= 憭拍?駁?皜豢?嚗?

?冽?嚗?
    python3 verse_archive_pipeline.py [max_results] [--no-move] [--headful]

    max_results   ??銝?嚗?閮?50
    --no-move     ?芸? EML+RAG嚗?蝘餃?靽∩辣嚗葫閰衣嚗????唬縑蝞梧?
    --headful     憿舐內?汗?刻?蝒??日?剁??身 headless嚗?
"""
import os, sys, re, json, hashlib, warnings, urllib.parse
from datetime import datetime, timedelta
import requests
from email.message import EmailMessage
import email.policy
from email.utils import formatdate
warnings.filterwarnings('ignore')  # ???折 SSL ??霅血?

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

sys.path.insert(0, os.path.expanduser("~/Claude/HCL"))
from project_keywords import match_project

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

# ?? 閮剖? ???????????????????????????????????????????????????????????????????
PORTAL_URL    = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL     = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME      = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD      = os.environ.get("HCL_PASSWORD",    "")
QDRANT_URL    = os.environ.get("QDRANT_URL",      "http://localhost:6333")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY",  "")

SOURCE_FOLDER = "04Done"
TARGET_FOLDER = "domdom"
COLLECTION    = "verse_emails"
VECTOR_SIZE   = 1536
OUTPUT_FILE   = "/tmp/verse_archive_pipeline_result.json"

# ?? ?閫?? ?????????????????????????????????????????????????????????????????
_args     = [a for a in sys.argv[1:] if not a.startswith("--")]
_flags    = {a for a in sys.argv[1:] if a.startswith("--")}
MAX_RESULTS = int(_args[0]) if _args else 50
NO_MOVE     = "--no-move" in _flags
HEADFUL     = "--headful" in _flags
OUTPUT_DIR  = os.path.expanduser("~/verse-export")
os.makedirs(OUTPUT_DIR, exist_ok=True)

qdrant        = QdrantClient(url=QDRANT_URL)
openai_client = OpenAI(api_key=OPENAI_KEY)


# ?? Qdrant / embedding / id ????????????????????????????????????????????????
def ensure_collection():
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        qdrant.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"撱箇? Qdrant collection: {COLLECTION}")


def make_id(subject, sender, date):
    return hashlib.md5(f"{sender}|{subject}|{date}".encode()).hexdigest()


def make_thread_id(subject):
    normalized = re.sub(
        r'^(??[:嚗\s*|RE[:嚗\s*|FW[:嚗\s*|Fwd[:嚗\s*)+',
        '', subject, flags=re.IGNORECASE
    ).strip()
    return hashlib.md5(normalized.encode()).hexdigest()


def id_to_uuid(h):
    h = h.ljust(32, "0")[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")  # text-embedding-3-* ?函?蝺函Ⅳ
except Exception:
    _ENC = None

EMBED_TOKEN_LIMIT = 8000  # 璅∪?銝? 8192嚗?蝺抵?

def get_embedding(text):
    # ?瑁?隢葡?航頞? 8192 token 銝? ??蝎暹??芣嚗iktoken 銝?冽??典??蝎摯嚗?
    if _ENC is not None:
        toks = _ENC.encode(text)
        if len(toks) > EMBED_TOKEN_LIMIT:
            text = _ENC.decode(toks[:EMBED_TOKEN_LIMIT])
    elif len(text) > EMBED_TOKEN_LIMIT * 2:
        text = text[:EMBED_TOKEN_LIMIT * 2]
    res = openai_client.embeddings.create(model="text-embedding-3-small", input=text)
    return res.data[0].embedding


# ?? ?交?甇???????????????????????????????????????????????????????????????????
# Verse ??.pim-mailread-sentdate 撠??縑隞嗆??撟港遢嚗? "Wed, Jun 10 9:13 AM"嚗?
# 銝?隢葡憿舐內????唬???????摰迤閬???ISO嚗撩撟港遢撠望蝞?
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

def normalize_sent_date(raw, today=None):
    """??Verse 憿舐內???銝脫迤閬???'YYYY-MM-DD HH:MM'嚗? 'YYYY-MM-DD'嚗瘜圾?? ''??""
    if not raw:
        return ""
    today = today or datetime.now()
    s = raw.replace("\n", " ").strip()

    # ?詨??伐?Today / Yesterday / Tomorrow
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

    # ??嚗??AM/PM ?葉??銝?/銝?嚗?
    hh = mm = None
    tm = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
    if tm:
        hh = int(tm.group(1)); mm = int(tm.group(2))
        is_pm = bool(re.search(r"PM|銝?", s, re.I))
        is_am = bool(re.search(r"AM|銝?", s, re.I))
        if is_pm and hh < 12: hh += 12
        if is_am and hh == 12: hh = 0

    # ?詨??亙????
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


# ?? ?餃 + ?脫?摰??冗 ??????????????????????????????????????????????????????
def login(page):
    page.goto(PORTAL_URL)
    page.wait_for_load_state("networkidle")
    page.fill('input[type="text"], input[placeholder*="Email"], input[name*="user"]', USERNAME)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"], button:has-text("?餃")')
    page.wait_for_load_state("networkidle")
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=15000)
    page.wait_for_timeout(2000)


def open_folder(page, folder_name):
    """
    暺?撌血鞈?憭暹邦銝剖???folder_name ???冗嚗??亙靽∩辣皜??
    Inbox ??撅?class `.inbox`嚗閮??冗嚗? 04Done嚗?賡???暺?
    銝?質??具??冗 / Folders??黎蝯ㄐ嚗?????
    """
    # 1) ??閰血????冗 / Folders?黎蝯??亙??其??芸???
    page.evaluate("""(name) => {
        const groups = [...document.querySelectorAll('[role="treeitem"], .folder-group, .nav-group')];
        for (const g of groups) {
            const t = (g.innerText || '').trim();
            if (/^(鞈?憭魯Folders|My Folders|?犖鞈?憭?/.test(t)) {
                const exp = g.getAttribute('aria-expanded');
                if (exp === 'false') {
                    const toggle = g.querySelector('[aria-expanded], .twisty, .expand') || g;
                    toggle.click();
                }
            }
        }
    }""", folder_name)
    page.wait_for_timeout(800)

    # 2) ?典椰?游??芸??曉 folder_name ??treeitem 銝阡?????喳 move popup嚗?
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
        # ??摮??亥?鞈?憭曉????踹?暺?怨府摮?靽∩辣???嗡??嚗?
        first_line = txt.split('\n')[0].strip()
        if folder_name in first_line:
            target = el
            break
    if target is None and n > 0:
        target = candidates.first  # fallback

    if target is None:
        # dump 撠 DOM 靘??
        nav_dump = page.evaluate("""() => {
            return [...document.querySelectorAll('[role="treeitem"]')]
                .map(el => (el.className || '') + ' :: ' + (el.innerText || '').split('\\n')[0].trim())
                .slice(0, 60);
        }""")
        raise RuntimeError(
            f"?曆??啗??冗?folder_name}???treeitem 皜嚗n  " +
            "\n  ".join(nav_dump)
        )

    target.scroll_into_view_if_needed()
    target.click()
    page.wait_for_timeout(1500)
    # 蝑縑隞嗆??桀?橘?蝛箄??冗????嚗策頛 timeout嚗?
    try:
        page.wait_for_selector('.seq-msg-row', timeout=8000)
    except Exception:
        pass
    # 蝑???摮葡??????"Subject" ?? ready嚗??憭?~6s
    for _ in range(12):
        try:
            if page.locator('.seq-msg-row').count() == 0:
                break  # 蝛箄??冗
            t = page.locator('.seq-msg-row').first.inner_text(timeout=1500)
            if "Subject" in t:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    page.wait_for_timeout(500)


# ?? 靽∩辣閫?? / 皜?嚗窒??index + export ?Ｘ??摩嚗????????????????????????????
UI_NOISE = {
    "More actions", "Mark as unread", "Mark as Needs Action",
    "Move to Trash", "Move to folder", "Open in new window",
    "Close", "Reply", "Reply All", "Forward", "Inbox",
    "Show more", "Show less", "Mark all as read", "Move all to Trash",
    "THREAD ACTIONS:", "Toggle message open/close",
}

_DATE_PATS = [
    re.compile(r'^\d{1,2}:\d{2}\s*(AM|PM)$'),
    re.compile(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b'),
    re.compile(r'^(Yesterday|Today|Tomorrow)\b'),
    re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+'),
]

def is_date_line(s):
    return any(p.match(s) for p in _DATE_PATS)


def clean_body(raw, subject):
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
    page.evaluate("""() => {
        const c = document.querySelector('.preview-container');
        if (!c) return;
        const btn = c.querySelector('.collapsed-recipient');
        if (btn) btn.click();
    }""")
    page.wait_for_timeout(500)
    return page.evaluate(r"""() => {
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
            to  = parsed.to.replace(/^me$/, 'shuhsing@ecic.com.tw');
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


# ?? ?辣銝? / EML ??嚗窒??export ?摩嚗????????????????????????????????????
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
        nm = (att['name'] or '').strip()
        # nm ?箇征???芣鋆詨瑼?嚗df/xlsx/docx...嚗?嚗敺?URL ??FileName= ??瑼?
        if not nm or re.fullmatch(r'[A-Za-z0-9]{2,5}', nm):
            try:
                nm = urllib.parse.unquote(att['href'].split('FileName=')[1].split('&')[0])
            except Exception:
                pass
        name = urllib.parse.unquote(nm) or "attachment"
        resp = session.get(att['href'], verify=False)
        if resp.status_code == 200:
            attachments.append((name, resp.content))
    return attachments


def _sent_date_to_rfc2822(sent_date):
    """??'YYYY-MM-DD HH:MM' ??'YYYY-MM-DD' 頧? RFC 2822 ?澆?嚗仃???喟征摮葡??""
    if not sent_date:
        return ''
    try:
        fmt = "%Y-%m-%d %H:%M" if len(sent_date) > 10 else "%Y-%m-%d"
        dt = datetime.strptime(sent_date, fmt)
        return formatdate(dt.timestamp(), localtime=True)
    except Exception:
        return ''


def pack_eml(meta, body, attachments):
    msg = EmailMessage(policy=email.policy.SMTP)
    msg['From']     = meta.get('from') or meta.get('sender', '')
    msg['To']       = meta.get('to') or USERNAME
    if meta.get('cc'):
        msg['Cc'] = meta['cc']
    msg['Subject']  = meta['subject']
    msg['Date']     = _sent_date_to_rfc2822(meta.get('sent_date', '')) or meta.get('date', '')
    msg['X-Source'] = 'HCL Verse / 04Done'
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    return msg.as_bytes()


def safe_filename(subject):
    return re.sub(r'[^\w銝-橦璞-_]', '_', subject)[:60]


# ?? 蝘餃 domdom嚗窒??move_construction v1.2.2 璈嚗??????????????????????????
def move_to_folder(page, folder=TARGET_FOLDER):
    """?身?格?靽∩辣撌脰◤?詨?嚗eading pane 撌脤?嚗?鞈?憭?icon ??頛詨?迂 ???詨???""
    # ?湔??瘥?靽∠??ove to folder???祈澈嚗lass ?箏?嚗?銝?鞈渡撅?action-tray-populated
    # 嚗nbox 瑼Ｚ??嗅惜??.action-tray-populated嚗??冗瑼Ｚ?瘝? ??銝?函撅斗?撠?
    MOVE_BTN_SEL = "button.action.pim-move-to-folder.icon"
    try:
        page.wait_for_selector(MOVE_BTN_SEL, timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(300)

    # ?閬????????梯?????button嚗?
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


# ?? 銝餅?蝔????????????????????????????????????????????????????????????????????
def process_current_email(page):
    """?格?靽∩辣撌脤?????header+body+?辣嚗???(email_dict, attachments)??""
    page.evaluate(
        "() => { [...document.querySelectorAll("
        "'.preview-container [aria-expanded=\"false\"]')].forEach(b => b.click()); }"
    )
    page.wait_for_timeout(800)

    header = extract_header_fields(page)
    raw    = page.locator('.preview-container').inner_text().strip()
    return header, raw


def main():
    ensure_collection()
    hindsight = HindsightClient(HINDSIGHT_URL)
    results = []
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADFUL, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)
        try:
            print("?餃 HCL Verse...")
            login(page)
            print(f"??鞈?憭整SOURCE_FOLDER}??..")
            open_folder(page, SOURCE_FOLDER)

            count = page.locator('.seq-msg-row').count()
            print(f"?SOURCE_FOLDER}??閬?{count} 撠?????嚗???{MAX_RESULTS}"
                  f"{'嚗?-no-move 銝宏?? if NO_MOVE else ''}嚗?..\n")

            processed = 0
            while processed < MAX_RESULTS:
                rows = page.locator('.seq-msg-row')
                if rows.count() == 0:
                    print("鞈?憭曉歇皜征嚗???)
                    break

                item = rows.first
                meta = None
                for _ in range(6):  # 擐??航?皜脫?嚗?閰?
                    meta = parse_msg_row(item)
                    if meta:
                        break
                    page.wait_for_timeout(600)
                    item = page.locator('.seq-msg-row').first
                if not meta:
                    print("  ?? ?⊥?閫??蝚砌????迫??)
                    break

                # 暺?
                item.click()
                page.wait_for_timeout(2000)

                header, raw = process_current_email(page)
                body      = clean_body(raw, meta["subject"])
                sender    = header.get("from") or meta["sender"]
                date_str  = header.get("date") or ""          # Verse 憿舐內??嚗?賜撩撟港遢嚗?
                sent_date = normalize_sent_date(date_str)      # 甇????ISO 撖辣??
                email_id  = make_id(meta["subject"], sender, date_str)

                # 摰?伐??仿?撌脰???嚗誨銵其?銝頛芰宏?仃??摰??券??剁????迫?踹??⊿?餈游?
                if email_id in seen_ids:
                    print(f"  ?? ?菜葫?圈?銴縑隞塚?蝘餃??航憭望?嚗??迫嚗meta['subject'][:40]}")
                    break
                seen_ids.add(email_id)

                email = {
                    "id": email_id, "subject": meta["subject"],
                    "snippet": meta["snippet"] or body[:200], "body": body,
                    "from": sender, "to": header.get("to", ""),
                    "cc": header.get("cc", ""), "bcc": header.get("bcc", ""),
                    "date": date_str, "sent_date": sent_date,
                    "thread_id": make_thread_id(meta["subject"]),
                    "label_ids": header.get("label_ids", []),
                }

                rec = {"subject": meta["subject"], "from": sender,
                       "date": date_str, "sent_date": sent_date}

                # ??RAG 蝝Ｗ?
                try:
                    text = f"{email['subject']} {email['body'] or email['snippet']}"
                    embedding = get_embedding(text)
                    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
                        id=id_to_uuid(email["id"]), vector=embedding, payload=email)])
                    rec["rag"] = "ok"
                except Exception as e:
                    rec["rag"] = f"fail: {e}"
                    print(f"  ??RAG 憭望?嚗e}")

                # ??EML ?臬
                try:
                    att_links   = get_attachment_links(page)
                    cookies     = {c['name']: c['value'] for c in page.context.cookies()}
                    attachments = download_attachments(att_links, cookies)
                    eml_bytes   = pack_eml(email, body, attachments)
                    eml_path    = os.path.join(OUTPUT_DIR, f"{safe_filename(meta['subject'])}.eml")
                    with open(eml_path, 'wb') as f:
                        f.write(eml_bytes)
                    rec["eml"] = eml_path
                    rec["attachments"] = [a[0] for a in attachments]
                except Exception as e:
                    rec["eml"] = f"fail: {e}"
                    attachments = []
                    print(f"  ??EML 憭望?嚗e}")

                # ??Hindsight retain
                try:
                    proj = match_project(email["subject"], email["body"][:500])
                    tags = ["source:verse"] + ([f"proj:{proj}"] if proj else [])
                    hindsight.retain(
                        content=f"銝餅嚗email['subject']}\n撖辣??{email['from']}\n?交?嚗email['sent_date']}\n\n{email['body']}",
                        document_id=email["id"],
                        timestamp=email["sent_date"],
                        tags=tags,
                        metadata={
                            "subject":   email["subject"],
                            "from":      email["from"],
                            "thread_id": email["thread_id"],
                            "eml_path":  rec.get("eml", ""),
                            "gmail_id":  "",
                            "label_ids": email["label_ids"],
                            "sent_date": email["sent_date"],
                        },
                        context=f"HCL Verse 靽∩辣嚗蜓?具email['subject']}??撖辣??{email['from']}",
                    )
                    rec["hindsight"] = f"ok (proj:{proj})"
                except Exception as e:
                    rec["hindsight"] = f"fail: {e}"
                    print(f"  ??Hindsight 憭望?嚗e}")

                # ??蝘餃 domdom
                if NO_MOVE:
                    rec["move"] = "skipped (--no-move)"
                    print(f"[{processed+1}] ??{meta['subject'][:40]} "
                          f"(RAG={rec.get('rag')}, ?辣{len(rec.get('attachments', []))}, 銝宏??")
                    results.append(rec)
                    processed += 1
                    # --no-move 璅∪??⊥?蝘餃嚗????????撠????芾??洵銝撠???
                    print("  嚗?-no-move 璅∪?嚗???桀??蝚砌?撠誑撽?瘚?嚗???")
                    break
                else:
                    status = move_to_folder(page, TARGET_FOLDER)
                    rec["move"] = status
                    flag = "?? if status == "moved" else "??
                    print(f"[{processed+1}] {flag} {meta['subject'][:40]} "
                          f"(RAG={rec.get('rag')}, ?辣{len(rec.get('attachments', []))}, move={status})")
                    if status != "moved":
                        results.append(rec)
                        print("  ??蝘餃?憭望?嚗?甇Ｖ誑??銴???)
                        break
                    page.wait_for_timeout(1500)  # 蝑??桀??

                results.append(rec)
                processed += 1

        finally:
            browser.close()

    moved        = sum(1 for r in results if r.get("move") == "moved")
    rag_ok       = sum(1 for r in results if r.get("rag") == "ok")
    hindsight_ok = sum(1 for r in results if (r.get("hindsight") or "").startswith("ok"))
    sent_dates = sorted(d[:10] for d in (r.get("sent_date") or "" for r in results) if d)
    summary = {
        "source": SOURCE_FOLDER, "target": TARGET_FOLDER,
        "no_move": NO_MOVE, "output_dir": OUTPUT_DIR,
        "archived_date": datetime.now().strftime("%Y-%m-%d"),  # 甇豢?嚗神?伐???
        "sent_date_range": (
            {"earliest": sent_dates[0], "latest": sent_dates[-1]} if sent_dates else None
        ),  # ?祆靽∩辣撖阡?撖辣?亦???
        "processed": len(results), "rag_ok": rag_ok, "hindsight_ok": hindsight_ok, "moved": moved,
        "emails": results,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n??摰?嚗???{len(results)} 撠?RAG {rag_ok} / Hindsight {hindsight_ok} ??嚗宏??{moved} 撠???{TARGET_FOLDER}")
    print(f"  蝯?撌脣神??{OUTPUT_FILE}")


if __name__ == "__main__":
    main()

