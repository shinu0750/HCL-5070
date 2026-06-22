#!/usr/bin/env python3
"""
閮芸恥?亙??唾?撖拇嚗??Verse ???隞嗅?整赤摰Ｗ撱隢?撖拇靽∩辣 ??
??暺? ??暺?LEAP 銵典?? ????? ?隞嗅??銝?撠?
?券??摰撓??JSON嚗汗?其?????蝣箄???
"""
import json, os

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

from playwright.sync_api import sync_playwright

PORTAL_URL = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL  = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME   = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD   = os.environ.get("HCL_PASSWORD",    "")
KEYWORD    = "閮芸恥?亙??唾?"
SIGN_FOLDER = "Sign"
OUTPUT     = "/tmp/hcl_admission.json"
MAX_MAILS  = 20  # 摰銝?


def login(page):
    page.goto(PORTAL_URL)
    page.wait_for_load_state("networkidle")
    page.fill('input[type="text"], input[placeholder*="Email"], input[name*="user"]', USERNAME)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"], button:has-text("?餃")')
    page.wait_for_load_state("networkidle")
    goto_inbox(page)


def goto_inbox(page):
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=15000)
    page.wait_for_timeout(2000)


def scroll_to_bottom(page):
    prev = 0
    for _ in range(30):
        page.evaluate("""() => {
            const els = [...document.querySelectorAll('*')].filter(el =>
                el.scrollHeight - el.clientHeight > 50 &&
                getComputedStyle(el).overflowY !== 'visible'
            );
            els.sort((a, b) => b.scrollHeight - a.scrollHeight);
            if (els[0]) els[0].scrollTop = els[0].scrollHeight;
            else window.scrollTo(0, document.body.scrollHeight);
        }""")
        page.wait_for_timeout(800)
        cur = page.locator('[role="treeitem"]').count()
        if cur == prev:
            break
        prev = cur


def parse_subject(item_text):
    lines = [l.strip() for l in item_text.split('\n') if l.strip()]
    subject = next((l for l in lines if KEYWORD in l), lines[0] if lines else "")
    return subject, lines


def approve_one(ctx, page, item):
    """暺?靽∩辣 ??暺?LEAP ?? ???詨?????(status, form_text, after_text)"""
    item.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    item.click()
    page.wait_for_timeout(3000)

    link = page.locator('a[href*="leap.ecic.com.tw"]').first
    form_page = page
    try:
        link.wait_for(state="visible", timeout=8000)
        with ctx.expect_page(timeout=15000) as new_page_info:
            link.click()
        form_page = new_page_info.value
    except Exception:
        try:
            link.click()
        except Exception as e:
            return f"error_no_link: {e}", "", ""
    form_page.wait_for_load_state("networkidle")
    form_page.wait_for_timeout(3000)

    try:
        form_text = form_page.locator("body").inner_text().strip()
    except Exception as e:
        form_text = f"(霈?”?桀仃?? {e})"
    print(f"--- 銵典 ({form_page.url}) ---", flush=True)
    print(form_text[:1500], flush=True)

    # ???蝎曄Ⅱ?寥?嚗?炊暺?瘨隢???
    try:
        approve_btn = form_page.get_by_role("button", name="?詨?", exact=True)
        if approve_btn.count() == 0:
            approve_btn = form_page.locator('button:text-is("?詨?"), input[value="?詨?"]')
        approve_btn.first.wait_for(state="visible", timeout=8000)
        approve_btn.first.click()
        form_page.wait_for_timeout(2000)
        for label in ["蝣箏?", "蝣箄?", "??, "OK"]:
            confirm = form_page.get_by_role("button", name=label, exact=True)
            if confirm.count() > 0 and confirm.first.is_visible():
                confirm.first.click()
                form_page.wait_for_timeout(2000)
                break
        form_page.wait_for_load_state("networkidle")
        form_page.wait_for_timeout(3000)
        after_text = form_page.locator("body").inner_text().strip()
        status = "approved" if "撌脤??拇?鈭? in after_text else f"unknown_response"
        print(f"  ?詨?蝯?嚗status}", flush=True)
    except Exception as e:
        return f"error_approve: {e}", form_text, ""

    # ??銵典??嚗?舀??嚗?
    if form_page is not page:
        try:
            form_page.close()
        except Exception:
            pass
    return status, form_text, after_text


def move_to_sign(page, subject):
    """?冽隞嗅?曉閰脖縑 ??暺? ??暺??冗 icon ??蝘餃 Sign嚗???hcl-move-construction ?摩嚗?""
    short = subject[:30]
    item = None
    for cand in page.locator(f'[role="treeitem"]:has-text("{KEYWORD}")').all():
        if short in cand.inner_text():
            item = cand
            break
    if item is None:
        print(f"  ??蝘餃?憭望?嚗隞嗅?曆??唬縑隞?, flush=True)
        return "move_not_found"

    item.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    item.click()

    MOVE_BTN_SEL = (
        "div.sticky-header > div.action-bar.collapse-stage-0.action-tray-populated > button.action.pim-move-to-folder.icon, "
        "div.action-bar.collapse-stage-0.action-tray-populated > button.action.pim-move-to-folder.icon"
    )
    try:
        page.wait_for_selector(MOVE_BTN_SEL, timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(300)

    move_btn = page.locator(MOVE_BTN_SEL).first
    if not move_btn.is_visible():
        print(f"  ??蝘餃?憭望?嚗銝鞈?憭?icon", flush=True)
        return "move_error_no_button"
    move_btn.click()
    page.wait_for_timeout(500)

    try:
        page.wait_for_selector("div.folder-tray-float.show", timeout=8000)
    except Exception:
        print(f"  ??蝘餃?憭望?嚗銝鞈?憭?popup", flush=True)
        return "move_error_no_popup"

    folder_input = page.locator("div.folder-tray-float.show input.folder-search-input")
    folder_input.click()
    folder_input.type(SIGN_FOLDER)
    page.wait_for_timeout(800)

    folder_item = page.locator("div.folder-tray-float.show [role='treeitem']:visible").first
    try:
        folder_item.wait_for(state="visible", timeout=5000)
        folder_item.click()
        page.wait_for_timeout(1500)
    except Exception:
        folder_input.press("Enter")
        page.wait_for_timeout(1500)

    print(f"  ??撌脩宏??{SIGN_FOLDER}", flush=True)
    return "moved"


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        login(page)

        for round_no in range(1, MAX_MAILS + 1):
            scroll_to_bottom(page)
            items = page.locator(f'[role="treeitem"]:has-text("{KEYWORD}")').all()
            # ?撌脰????蜓??
            done_subjects = {r["subject"] for r in results}
            pending = []
            for it in items:
                subj, _ = parse_subject(it.inner_text().strip())
                if subj not in done_subjects:
                    pending.append((it, subj))
            if round_no == 1:
                print(f"?曉 {len(pending)} 撠?KEYWORD}??敺祟?訾縑隞?, flush=True)
            if not pending:
                break

            item, subject = pending[0]
            print(f"\n[{len(results)+1}] {subject[:60]}", flush=True)
            status, form_text, after_text = approve_one(ctx, page, item)

            # ?詨??? ???隞嗅?縑蝘餃 Sign
            move_status = "skipped"
            goto_inbox(page)
            if status == "approved":
                scroll_to_bottom(page)
                move_status = move_to_sign(page, subject)
                goto_inbox(page)

            results.append({"subject": subject, "status": status,
                            "move_status": move_status,
                            "form_text": form_text, "after_text": after_text})

        approved = sum(1 for r in results if r["status"] == "approved")
        moved = sum(1 for r in results if r.get("move_status") == "moved")
        print(f"\n摰?嚗approved}/{len(results)} 撠????{moved} 撠歇蝘餃 {SIGN_FOLDER}", flush=True)

        with open(OUTPUT, "w") as f:
            json.dump({"keyword": KEYWORD, "total": len(results),
                       "approved": approved, "moved": moved, "results": results}, f,
                      ensure_ascii=False, indent=2)
        print(f"蝯?撌脣神??{OUTPUT}嚗汗?其?????, flush=True)

        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass


if __name__ == "__main__":
    main()

