#!/usr/bin/env python3
"""
fetch_verse_month.py
把指定年月的 HCL Verse 信件（含完整 body）抓取為 JSON。
輸出格式與 process_one_by_one.py 的 input_threads.json 相同。

用法（Windows Python，需有 Playwright + Edge）：
  python fetch_verse_month.py --year 2025 --month 1 --output verse_2025_01.json
  python fetch_verse_month.py --year 2025 --month 1 --dry-run   (只列 UNID，不開信)

Prerequisites：
  pip install playwright beautifulsoup4
  playwright install msedge
  ~/.hermes/.env 裡有 HCL_PORTAL_URL / HCL_VERSE_URL / HCL_USERNAME / HCL_PASSWORD
"""

import argparse, json, os, re, ssl, sys, urllib.request
from datetime import datetime, timezone, timedelta

# ── Playwright 只在 Windows 有，先 import ──────────────────────────────────
from playwright.sync_api import sync_playwright

# ── 環境設定 ──────────────────────────────────────────────────────────────
_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

PORTAL_URL = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL   = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME    = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD    = os.environ.get("HCL_PASSWORD",    "")
INBOX_API   = "https://mail1.ecic.com.tw/mail/6971.nsf/pob/api/search/inbox"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

TZ_TAIPEI = timezone(timedelta(hours=8))


# ── 工具函式 ──────────────────────────────────────────────────────────────

def cookies_to_header(cookies):
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def api_get_json(url, cookie_str):
    req = urllib.request.Request(url, headers={
        "Cookie": cookie_str,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_domino_email(raw):
    """
    把 Domino 格式的寄件者字串轉成 email 或顯示名稱。
    'CN=孫哲仁/OU=台北/O=永光化學' → 孫哲仁
    '"andl@ecic.com.tw" <andl@ecic.com.tw>' → andl@ecic.com.tw
    'johnnysun@ecic.com.tw' → 直接回傳
    """
    if not raw:
        return ""
    raw = raw.strip().strip('"')
    # 找 <email> 格式
    m = re.search(r'<([\w.+-]+@[\w.-]+)>', raw)
    if m:
        return m.group(1)
    # 直接 email
    if re.match(r'^[\w.+-]+@[\w.-]+$', raw):
        return raw
    # CN=姓名/OU=.../O=... → 取 CN= 後的名稱
    m = re.match(r'CN=([^/]+)', raw)
    if m:
        return m.group(1).strip()
    # 其他帶 @ 但不是標準格式的（如 everlight@everlight）
    if "@" in raw:
        return raw.split("@")[0].strip()
    return raw


def extract_recipients(lst):
    """list of 'CN=xxx@yyy' 或 email 字串 → list of email 字串"""
    result = []
    for item in (lst or []):
        email = parse_domino_email(item)
        if email:
            result.append(email)
    return result


def parse_mail_date(maildate_str):
    """'2025-01-15T08:30:00.000Z' → ISO 8601 with UTC"""
    try:
        dt = datetime.fromisoformat(maildate_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except Exception:
        return maildate_str


# ── Step 1: 從 API 抓指定月份的 UNID 清單 ────────────────────────────────

def fetch_inbox_mails_for_month(cookie_str, year, month):
    """
    分頁拉 inbox API，回傳屬於 year/month 的 mail metadata list。
    每筆包含：unid, thread_id (tua0), subject, maildate, sender, toRecipients, ccRecipients
    """
    target_ym = (year, month)
    mails = []
    start = 0
    rows = 50
    seen_unids = set()

    print(f"  拉 inbox API (year={year}, month={month})...")

    while True:
        url = (
            f"{INBOX_API}?start={start}&rows={rows}&softdeletion=0&thread=0"
            f"&withunread=0&timezone=Asia%2FTaipei&altername=1&xhr=1&sq=1"
        )
        try:
            data = api_get_json(url, cookie_str)
        except Exception as e:
            print(f"  API 錯誤 start={start}: {e}")
            break

        docs = data.get("response", {}).get("docs", [])
        if not docs:
            print(f"  API 無更多資料 (start={start})")
            break

        found_in_range = 0
        past_range = 0

        for doc in docs:
            unid = doc.get("unid", "")
            if unid in seen_unids:
                continue
            seen_unids.add(unid)

            maildate = doc.get("maildate", "")
            try:
                dt = datetime.fromisoformat(maildate.replace("Z", "+00:00"))
                doc_ym = (dt.year, dt.month)
            except Exception:
                continue

            if doc_ym == target_ym:
                found_in_range += 1
                mail = {
                    "unid":         unid,
                    "thread_id":    doc.get("tua0", unid),
                    "subject":      doc.get("subject", ""),
                    "maildate":     parse_mail_date(maildate),
                    "sender":       parse_domino_email(doc.get("inetfrom") or doc.get("altfrom") or ""),
                    "toRecipients": extract_recipients(doc.get("sendto", [])),
                    "ccRecipients": extract_recipients(doc.get("altsendto", []) + doc.get("inetcopyto", [])),
                    "altfrom":      doc.get("altdisplayname", ""),
                    "abstract":     doc.get("abstract", ""),
                }
                mails.append(mail)
            elif doc_ym < target_ym:
                past_range += 1

        print(f"  start={start}: {len(docs)} 筆, 當月 {found_in_range}, 更早 {past_range}")

        # 如果有超過一半是更早的信，停止（inbox 是最新到最舊）
        if past_range > len(docs) * 0.8:
            print(f"  已過目標月份，停止分頁")
            break

        start += rows

        # 保護：最多 2000 封
        if start > 2000:
            print(f"  已查詢 2000 筆，停止")
            break

    print(f"  找到 {len(mails)} 封 {year}/{month:02d} 的信件")
    return mails


# ── Step 2: Playwright 登入 Verse ─────────────────────────────────────────

def verse_login(page):
    page.goto(PORTAL_URL)
    page.wait_for_load_state("networkidle")
    page.fill('input[type="text"], input[placeholder*="Email"], input[name*="user"]', USERNAME)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"], button:has-text("登入")')
    page.wait_for_load_state("networkidle")
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=20000)
    page.wait_for_timeout(3000)
    print("  Verse 登入成功")


# ── Step 3: 用 UNID 點開信件，讀取 body ──────────────────────────────────

def scroll_inbox_to_load_unid(page, unid):
    """捲動 inbox list 直到 UNID 對應的 msg-info 出現在 DOM"""
    sel = f'[id="{unid}-msg-info"]'
    max_scrolls = 30

    for i in range(max_scrolls):
        if page.locator(sel).count() > 0:
            return True
        # 捲動 inbox list（messageList 容器）
        page.evaluate("""() => {
            const list = document.querySelector('.message-list-container, .messageList, ol.react-message-list');
            if (list) list.scrollTop += 1000;
            else window.scrollBy(0, 1000);
        }""")
        page.wait_for_timeout(600)

    return page.locator(sel).count() > 0


def fetch_body_by_unid(page, unid):
    """
    用 UNID 找到 msg-info，點擊父 LI，讀取 pim-mailread-mailcontent。
    回傳 (body_text, attachment_names)
    """
    sel = f'[id="{unid}-msg-info"]'

    if page.locator(sel).count() == 0:
        if not scroll_inbox_to_load_unid(page, unid):
            return None, []

    dl = page.locator(sel).first
    li = dl.locator('xpath=..')
    li.scroll_into_view_if_needed()
    li.click()
    page.wait_for_timeout(3000)

    # 等讀信面板（非 collapsed）
    body_text = None
    for attempt in range(3):
        try:
            body_text = page.evaluate("""() => {
                // 優先取非 collapsed 的 body
                const els = document.querySelectorAll('.pim-mailread-mailcontent');
                let best = { len: 0, text: '' };
                els.forEach(el => {
                    const cls = el.className || '';
                    const t = (el.textContent || '').trim();
                    if (!cls.includes('collapsed') && t.length > best.len) {
                        best = { len: t.length, text: t };
                    }
                });
                // 退而求其次：任何 pim-mailread-mailcontent
                if (!best.text) {
                    els.forEach(el => {
                        const t = (el.textContent || '').trim();
                        if (t.length > best.len) best = { len: t.length, text: t };
                    });
                }
                return best.text;
            }""")
            if body_text and len(body_text) > 10:
                break
        except Exception:
            pass
        page.wait_for_timeout(2000)

    # 收集附件名稱（從讀信面板內找有副檔名的連結文字）
    attachments = []
    try:
        att_names = page.evaluate("""() => {
            const atts = [];
            // 讀信面板容器
            const pane = document.querySelector(
                '.preview-container, .preview-panel, .pim-mailread-container:not(.collapsed-pim)'
            );
            if (!pane) return [];
            // 找包含副檔名的文字節點
            const ext = new RegExp('[.](pdf|docx?|xlsx?|pptx?|zip|png|jpg|jpeg|gif|txt|csv|xml|msg|eml)$', 'i');
            pane.querySelectorAll('a, span, div').forEach(el => {
                const t = (el.textContent || '').trim();
                if (ext.test(t) && t.length < 200 && !t.includes('\n')) {
                    atts.push(t);
                }
            });
            return [...new Set(atts)];
        }""")
        if att_names:
            attachments = [{"filename": n, "mimeType": ""} for n in att_names]
    except Exception:
        pass

    return body_text or "", attachments


# ── Step 4: 組裝成 input_threads.json 格式 ───────────────────────────────

def build_threads(mails_with_body):
    """
    按 thread_id (tua0) 分組，輸出 thread list。
    每個 thread 裡的 messages 按 maildate 排序。
    """
    thread_map = {}
    for m in mails_with_body:
        tid = m["thread_id"]
        if tid not in thread_map:
            thread_map[tid] = []
        thread_map[tid].append({
            "id":            m["unid"],
            "date":          m["maildate"],
            "subject":       m["subject"],
            "sender":        m["sender"],
            "toRecipients":  m["toRecipients"],
            "ccRecipients":  m["ccRecipients"],
            "attachments":   m.get("attachments", []),
            "plaintextBody": m.get("body") or "",
        })

    threads = []
    for tid, messages in thread_map.items():
        messages.sort(key=lambda x: x["date"])
        threads.append({"id": tid, "messages": messages})

    # 整個 list 按最新信件日期排序（新到舊）
    threads.sort(key=lambda t: t["messages"][-1]["date"], reverse=True)
    return threads


# ── 主程式 ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch HCL Verse mails for a given month")
    parser.add_argument("--year",    type=int, required=True)
    parser.add_argument("--month",   type=int, required=True)
    parser.add_argument("--output",  default="verse_fetch.json")
    parser.add_argument("--dry-run", action="store_true", help="只列 UNID，不打開每封信")
    parser.add_argument("--limit",   type=int, default=0, help="只處理前 N 封（0=全部）")
    args = parser.parse_args()

    print(f"[Verse Fetch] {args.year}/{args.month:02d}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # Login
        verse_login(page)
        cookies = ctx.cookies()
        ck = cookies_to_header(cookies)

        # Step 1: 拿 UNID 清單
        mails = fetch_inbox_mails_for_month(ck, args.year, args.month)

        if not mails:
            print("沒有找到任何信件")
            browser.close()
            return

        print(f"\n共 {len(mails)} 封，開始逐一取 body...")

        target_mails = mails[:args.limit] if args.limit > 0 else mails

        if args.dry_run:
            for m in target_mails:
                print(f"  {m['maildate'][:10]} | {m['unid'][:12]} | {m['subject'][:40]}")
            print("\n(dry-run，跳過開信)")
        else:
            # Step 2: 逐一開信取 body
            for i, m in enumerate(target_mails):
                print(f"  [{i+1}/{len(target_mails)}] {m['maildate'][:10]} {m['subject'][:40]}")
                try:
                    body, atts = fetch_body_by_unid(page, m["unid"])
                    m["body"] = body
                    m["attachments"] = atts
                    if body:
                        print(f"    OK body={len(body)} chars, atts={len(atts)}")
                    else:
                        # fallback：用 API 的 abstract
                        abstract = m.get("abstract", "")
                        m["body"] = abstract
                        m["body_from_abstract"] = True
                        print(f"    FALLBACK abstract={len(abstract)} chars")
                except Exception as e:
                    print(f"    ERROR: {e}")
                    m["body"] = ""
                    m["attachments"] = []

        browser.close()

    # Step 3: 組裝並輸出
    threads = build_threads(target_mails)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(threads, f, ensure_ascii=False, indent=2)

    total_msgs = sum(len(t["messages"]) for t in threads)
    print(f"\n輸出 {args.output}")
    print(f"  {len(threads)} threads, {total_msgs} messages")
    print(f"\n下一步：")
    print(f"  cd /home/eid/scripts/email-to-hindsight")
    print(f"  python process_one_by_one.py --input-json {args.output}")


if __name__ == "__main__":
    main()
