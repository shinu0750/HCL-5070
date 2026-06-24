#!/usr/bin/env python3
"""
?隞嗅?曉蝚血??摮?靽∩辣嚗閮?銝莎?嚗?蝘餃??5Other?????
"""
import json, os, sys
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

PORTAL_URL    = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL     = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME      = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD      = os.environ.get("HCL_PASSWORD",    "")
TARGET_FOLDER = "05Other"
SIGN_FOLDER   = "Sign"
KEYWORD_FOLDER_MAP = {"已離廠": SIGN_FOLDER, "已入廠": SIGN_FOLDER}
KEYWORDS      = ["入廠施工", "SCI 安全氣候指標", "溶劑採購通知", "假日施工單申請已核可", "火警警報", "電梯安檢", "已離廠", "已入廠", "請問本週假日 是否有安排假日工程"]
DATE_LABELS   = {"寄件者", "主旨", "訊息摘要", "今天", "昨天", "本週", "上週", "更早", "本月"}


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


# ?? Virtual-scroll ?澆捆??極?瘀?v1.2.0嚗?????????????????????????????????
# Verse ?嗡辣?? virtual scroll嚗OM ?芯??閬? 簣 buffer嚗???◤???
# ???歲?啣???locator.all()??賜??啁銝葡?? ~30 撠?120 撠?inbox ??憭批???
# ?寧??????脣?嚗??? DOM 蝝舐??駁?嚗摨???? N ??唬蜓?冽???

_LARGEST_SCROLLER_JS = """
    const els = [...document.querySelectorAll('*')].filter(el =>
        el.scrollHeight - el.clientHeight > 50 &&
        getComputedStyle(el).overflowY !== 'visible'
    );
    els.sort((a, b) => b.scrollHeight - a.scrollHeight);
    return els[0] || document.scrollingElement;
"""


def scroll_to_top(page):
    """?脣??”?嚗Ⅱ靽????唬縑隞嗆? click ?賣迤蝣箄孛?潮霈蝒"""
    page.evaluate(f"(() => {{ const el = (() => {{ {_LARGEST_SCROLLER_JS} }})(); el.scrollTop = 0; }})()")
    page.wait_for_timeout(800)


def _scroll_down_page(page, ratio=0.85):
    """敺銝蝝???摨佗???15% ???踹?瞍???靽∩辣嚗"""
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_LARGEST_SCROLLER_JS} }})();
        el.scrollTop = el.scrollTop + el.clientHeight * {ratio};
        return el.scrollTop;
    }})()""")


def _scroller_at_bottom(page):
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_LARGEST_SCROLLER_JS} }})();
        return el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
    }})()""")


def scroll_to_bottom(page):
    """???脣摨??澆捆 virtual scroll嚗?蝯衣宏??畾菟???DOM ?具"""
    scroll_to_top(page)
    for _ in range(60):
        _scroll_down_page(page)
        page.wait_for_timeout(700)
        if _scroller_at_bottom(page):
            break


def _scan_visible_matches(page, seen):
    """???DOM嚗??單?潛??[{sender, subject, keyword, folder}, ...]嚗歇閬??歲?"""
    new_items = []
    for item in page.locator('[role="treeitem"]').all():
        try:
            text = item.inner_text(timeout=2000).strip()
        except Exception:
            continue
        key = text[:100]
        if key in seen:
            continue
        kw_hit = next((k for k in KEYWORDS if k in text), None)
        if not kw_hit:
            continue
        seen.add(key)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        subject = next((l for l in lines if kw_hit in l), lines[0] if lines else "")
        sender = next((l for l in lines
                       if l and l not in DATE_LABELS
                       and not l.isdigit()
                       and not any(k in l for k in KEYWORDS)), "")
        folder = KEYWORD_FOLDER_MAP.get(kw_hit, TARGET_FOLDER)
        new_items.append({"sender": sender, "subject": subject, "keyword": kw_hit, "folder": folder})
    return new_items


def collect_subjects(page, max_pages=80):
    """
    敺??券??脣?嚗?????DOM 蝝舐?蝚血??摮?靽∩辣??
    ?迫璇辣嚗?啣?嚗scroller_at_bottom嚗R ??max_pages 銝???
    v1.2.1嚗宏?扎?? N ??唬蜓?典停???拙??摩 ??蝚血?銝餅?航蝔??撣
    inbox 銝剜挾?偏畾蛛??嗾???賭葉銝誨銵典??Ｘ???敹????脣摨??賭?蝯???
    """
    scroll_to_top(page)
    page.wait_for_timeout(1000)

    seen = set()
    items = []
    for page_idx in range(1, max_pages + 1):
        new_items = _scan_visible_matches(page, seen)
        items.extend(new_items)
        if new_items:
            print(f"  [scan] 蝚?{page_idx} ???啣? {len(new_items)} 撠?蝝航? {len(items)}嚗")

        if _scroller_at_bottom(page):
            print(f"  [scan] 撌脫?啣?嚗 {page_idx} ??{len(items)} 撠泵??")
            break

        _scroll_down_page(page)
        page.wait_for_timeout(700)
    else:
        print(f"  [scan] ????{max_pages} ???芸摨?撌脫 {len(items)} 撠?")

    return items


def _scan_for_match(page, short, keyword):
    """???DOM ?曄泵??(short, keyword) ??treeitem?"""
    for item in page.locator(f'[role="treeitem"]:has-text("{keyword}")').all():
        try:
            if short in item.inner_text(timeout=2000):
                return item
        except Exception:
            continue
    return None


def find_item_in_inbox(page, subject, keyword, max_pages=80):
    """
    v1.2.0嚗?????脣??曆縑隞塚??澆捆 virtual scroll嚗?
    ???芰??嗡? DOM 銝??120 撠?inbox 銝剝?敺?靽∩辣?銝??
    """
    short = subject[:30]
    scroll_to_top(page)
    page.wait_for_timeout(500)
    for _ in range(max_pages):
        item = _scan_for_match(page, short, keyword)
        if item is not None:
            try:
                item.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            return item
        if _scroller_at_bottom(page):
            break
        _scroll_down_page(page)
        page.wait_for_timeout(600)
    fallback = page.locator(f'[role="treeitem"]:has-text("{keyword}")').first
    return fallback if fallback.count() else None


def move_to_folder(page, item, sender, subject, folder=TARGET_FOLDER):
    """暺?靽∩辣 ??暺??冗 icon ??頛詨?格?鞈?憭???Enter"""
    item.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    item.click()

    # ?典??渲楝敺??圈???? button
    # ?桀?靽?DOM嚗iv.sticky-header > div.action-bar.collapse-stage-0.action-tray-populated > button
    # 閮?銝?DOM嚗iv.sticky-header > div > button.action.pim-move-to-folder.icon 嚗???action-bar class嚗?
    MOVE_BTN_SEL = (
        "div.sticky-header > div.action-bar.collapse-stage-0.action-tray-populated > button.action.pim-move-to-folder.icon, "
        "div.action-bar.collapse-stage-0.action-tray-populated > button.action.pim-move-to-folder.icon, "
        "div.sticky-header > div > button.action.pim-move-to-folder.icon"
    )
    try:
        page.wait_for_selector(MOVE_BTN_SEL, timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(300)

    move_btn = page.locator(MOVE_BTN_SEL).first
    if not move_btn.is_visible():
        print(f"  ???曆??啗??冗 icon嚗{sender} / {subject[:40]}")
        return "error_no_button"
    move_btn.click()
    page.wait_for_timeout(500)

    try:
        page.wait_for_selector("div.folder-tray-float.show", timeout=8000)
    except Exception:
        print(f"  ???曆??啗??冗 popup嚗{sender} / {subject[:40]}")
        return "error_no_popup"

    folder_input = page.locator("div.folder-tray-float.show input.folder-search-input")
    folder_input.click()
    # v1.2.2嚗? fill 皜征 + ??type 閫貊 React onChange嚗elay=50 ?踹? IME 畾?
    folder_input.fill("")
    folder_input.type(folder, delay=50)
    page.wait_for_timeout(1000)

    # v1.2.2嚗?蝣箸?撠??冗?迂 ??????.first嚗?瞈暹??????賊?
    # 撠? moved 雿祕??蝘餃?嚗葉??蝔梯??冗撠文撣貉?嚗?
    folder_item = page.locator(
        f"div.folder-tray-float.show [role='treeitem']:visible:has-text('{folder}')"
    ).first
    try:
        folder_item.wait_for(state="visible", timeout=5000)
        folder_item.click()
        page.wait_for_timeout(1500)
    except Exception:
        print(f"  ?? ?曆??啣?{folder}???嚗??Enter")
        folder_input.press("Enter")
        page.wait_for_timeout(1500)

    # v1.2.2嚗opup 瘝? = 蝘餃?憭望?
    if page.locator("div.folder-tray-float.show").count() > 0:
        print(f"  ?? 鞈?憭?popup ?芷???鋆? Enter嚗{sender} / {subject[:40]}")
        try:
            folder_input.press("Enter")
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if page.locator("div.folder-tray-float.show").count() > 0:
            return "error_popup_stuck"

    print(f"  ??撌脩宏??{sender} / {subject[:50]}")
    return "moved"


def main():
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        login(page)

        # v1.2.0嚗ollect_subjects ?折??????脣???嚗????scroll_to_bottom
        mails = collect_subjects(page)
        scroll_to_top(page)
        kw_str = "?".join(KEYWORDS)
        print(f"?曉 {len(mails)} 撠泵???萄?嚗{kw_str}嚗?靽∩辣嚗?憪宏??{TARGET_FOLDER}??..\n")

        for i, mail in enumerate(mails, 1):
            sender  = mail["sender"]
            subject = mail["subject"]
            print(f"[{i}/{len(mails)}] {sender} - {subject[:50]}")

            # 瘥活蝘餃????唳隞嗅嚗ind_item_in_inbox ?銵???脣?格?雿蔭
            if i > 1:
                page.goto(VERSE_URL)
                page.wait_for_load_state("networkidle")
                page.wait_for_selector('[role="treeitem"]', timeout=15000)
                page.wait_for_timeout(1500)

            item_el = find_item_in_inbox(page, subject, mail["keyword"])
            if item_el is None:
                print(f"  ???曆??唬縑隞塚??航撌脩宏??嚗{subject[:50]}")
                results.append({**mail, "status": "not_found"})
                continue

            status = move_to_folder(page, item_el, sender, subject, mail["folder"])
            results.append({**mail, "status": status})

        browser.close()

    moved   = sum(1 for r in results if r["status"] == "moved")
    errors  = sum(1 for r in results if r["status"] != "moved")
    print(f"\n摰?嚗{moved} 撠歇蝘餃?嚗{errors} 撠仃?")

    output = {"total": len(mails), "moved": moved, "results": results}
    out_path = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "hcl_move_construction.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

