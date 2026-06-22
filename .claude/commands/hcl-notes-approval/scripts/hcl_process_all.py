#!/usr/bin/env python3
"""
HCL Notes 蝪賣?芸?????摰瘚?嚗ndroid ??

  Phase 1 (Playwright) : ?? HCL Verse ?嗡辣????曉敺偷?訾縑隞塚?蝘餃 Unsigned 鞈?憭?
  Phase 2 (Android)    : ?? ADB ?? Android 璅⊥?剁????? Nomad 銵典銝行??
  Phase 3 (Playwright) : 撠?Unsigned 銝剖歇???縑隞嗥宏??Sign 鞈?憭?
"""

# ?? ?啣?霈頛 ?????????????????????????????????????????????????????????????
import os, json, re, subprocess, sys, time

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
VERSE_URL   = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME    = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD    = os.environ.get("HCL_PASSWORD",    "")

APPROVAL_KEYWORDS = ["憭??, "??唾?", "?芸?∪", "憭?", "隢???]


# ????????????????????????????????????????????????????????????????????????????????
# ?梁 Playwright 撌亙
# ????????????????????????????????????????????????????????????????????????????????

def _login(page):
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


def _largest_scroller_js():
    """JS嚗?箸隞嗅??脣?摰孵嚗?憭抒? overflow-y!=visible ??嚗?""
    return """
        const els = [...document.querySelectorAll('*')].filter(el =>
            el.scrollHeight - el.clientHeight > 50 &&
            getComputedStyle(el).overflowY !== 'visible'
        );
        els.sort((a, b) => b.scrollHeight - a.scrollHeight);
        return els[0] || document.scrollingElement;
    """


def _scroll_to_top(page):
    page.evaluate(f"(() => {{ const el = (() => {{ {_largest_scroller_js()} }})(); el.scrollTop = 0; }})()")
    page.wait_for_timeout(800)


def _scroll_down_page(page, ratio=0.85):
    """敺銝蝝???摨佗???15% ???踹?瞍???靽∩辣嚗??單??scrollTop??""
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_largest_scroller_js()} }})();
        el.scrollTop = el.scrollTop + el.clientHeight * {ratio};
        return el.scrollTop;
    }})()""")


def _scroller_at_bottom(page):
    """JS嚗?瑟隞嗅?脣?璇?血歇?啣?嚗捆撌?5px嚗?""
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_largest_scroller_js()} }})();
        return el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
    }})()""")


# 靽???蝔曹??嗡??澆?摰對?_reload_inbox嚗?雿?箝摰??????桃?頝喳摨?
def _scroll_to_bottom(page):
    """??敺銝?啣?嚗摰?virtual scroll嚗??澆?嚗?""
    _scroll_to_top(page)
    for _ in range(60):
        _scroll_down_page(page)
        page.wait_for_timeout(700)
        if _scroller_at_bottom(page):
            break


# ????????????????????????????????????????????????????????????????????????????????
# Phase 1 ?????嗡辣??蒂蝘餃 Unsigned嚗laywright嚗?
# ????????????????????????????????????????????????????????????????????????????????

def _move_email_to_folder(page, item, folder_name):
    """撠縑隞嗥宏?唳?摰??冗嚗? Phase 3 ?詨?璈嚗?""
    item.click()
    try:
        page.wait_for_selector("div.action-tray-populated", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    # ?曄宏????collapse-stage-0 class 蝣箔??賭葉?航???嚗?
    move_btn = page.locator(
        "div.action-tray-populated button.action.pim-move-to-folder.icon.collapse-stage-0"
    )
    if not move_btn.is_visible():
        # fallback嚗?撣?collapse-stage-0
        move_btn = page.locator(
            "div.action-tray-populated button.action.pim-move-to-folder.icon"
        ).first
    if not move_btn.is_visible():
        return "error_no_button"
    move_btn.click()
    page.wait_for_timeout(500)

    try:
        page.wait_for_selector("div.folder-tray-float.show", timeout=8000)
    except Exception:
        return "error_no_popup"

    folder_input = page.locator("div.folder-tray-float.show input.folder-search-input")
    folder_input.click()
    # v1.3.4嚗? fill 皜征 + ??type 閫貊 React onChange嚗elay=50 ?踹? IME 畾?
    folder_input.fill("")
    folder_input.type(folder_name, delay=50)
    page.wait_for_timeout(1000)

    # v1.3.4嚗?蝣箸?撠??冗?迂 ??????.first ??Chinese folder name ???賊?
    # 撠? moved 雿祕??蝘餃?嚗eeting/construction 撌脣祕皜砌葉??bug嚗?
    sign_item = page.locator(
        f"div.folder-tray-float.show [role='treeitem']:visible:has-text('{folder_name}')"
    ).first
    try:
        sign_item.wait_for(state="visible", timeout=5000)
        sign_item.click()
    except Exception:
        folder_input.press("Enter")
    page.wait_for_timeout(1500)

    # v1.3.4嚗opup 瘝? = 蝘餃?憭望?
    if page.locator("div.folder-tray-float.show").count() > 0:
        try:
            folder_input.press("Enter")
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if page.locator("div.folder-tray-float.show").count() > 0:
            return "error_popup_stuck"
    return "moved"


def _reload_inbox(page):
    """?頛?嗡辣??蒂?脣摨"""
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=15000)
    page.wait_for_timeout(1500)
    _scroll_to_bottom(page)


def _locate_inbox_item(page, sender, subject):
    """?函??銵其葉?摰???靽∩辣??嚗宏?? DOM ??堆???摰?嚗?""
    kw = next((k for k in APPROVAL_KEYWORDS if k in subject), None)
    sel = f'[role="treeitem"]:has-text("{kw}")' if kw else '[role="treeitem"]'
    for item in page.locator(sel).all():
        try:
            text = item.inner_text(timeout=3000)
        except Exception:
            continue
        if subject in text and (not sender or sender in text):
            return item
    return None


def _find_item_by_scroll(page, sender, subject, max_pages=80):
    """
    Verse ??virtual scrolling嚗OM ?芯??閬? 簣 buffer嚗???◤???
    ?砍撘??????敺銝嚗??炎??(sender, subject) ?臬?函??DOM 銝准?
    ??曉??Locator嚗銝? None??
    """
    _scroll_to_top(page)
    page.wait_for_timeout(500)
    for _ in range(max_pages):
        item = _locate_inbox_item(page, sender, subject)
        if item is not None:
            try:
                item.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            return item
        if _scroller_at_bottom(page):
            return None
        _scroll_down_page(page)
        page.wait_for_timeout(600)
    return None


def _parse_treeitem(text):
    """敺?treeitem inner_text 閫?? (sender, subject)????(sender, subject) ??(None, None)??""
    DATE_LABELS = {"撖辣??, "銝餅", "閮??", "隞予", "?典予", "?祇?, "銝?, "?湔"}
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return None, None
    subject = next((l for l in lines if any(k in l for k in APPROVAL_KEYWORDS)),
                   lines[0])
    sender = next((l for l in lines
                   if l and l not in DATE_LABELS
                   and not any(k in l for k in APPROVAL_KEYWORDS)), "")
    return sender, subject


def _scan_visible_matches(page, seen):
    """
    ???嗅? DOM 銝剜???[role="treeitem"]嚗??單?潛??[(sender, subject), ...]??
    撌脣 seen set 銝剔??歲??seen ?停?唳?啜?
    """
    new_matches = []
    for item in page.locator('[role="treeitem"]').all():
        try:
            text = item.inner_text(timeout=2000).strip()
        except Exception:
            continue
        sender, subject = _parse_treeitem(text)
        if not subject:
            continue
        # ?芣銝餅?恍??萄???
        if not any(k in subject for k in APPROVAL_KEYWORDS):
            continue
        key = (sender, subject)
        if key in seen:
            continue
        seen.add(key)
        new_matches.append((sender, subject))
    return new_matches


def _scroll_and_collect_all(page, no_new_limit=50):
    """
    敺??券?憪??脣?嚗????閬縑隞嗡蒂蝝舐?蝚血??摮?
    ?迫璇辣嚗crollTop 蝣箏祕瘝?蝘餃?嚗?甇?摨???
    no_new_limit 靽??箔??芰嚗?閮?50嚗???~120 撠隞嗅嚗?
    ? [{sender, subject, category}, ...]
    """
    print("  敺??券???嚗摰?virtual scrolling嚗?..", flush=True)
    _scroll_to_top(page)
    page.wait_for_timeout(1200)

    seen = set()
    results = []
    no_new = 0
    page_idx = 0
    prev_scroll_top = -1

    while True:
        new_matches = _scan_visible_matches(page, seen)
        for sender, subject in new_matches:
            if "撌脫?? in subject or "撌脫?? in subject:
                category = "?詨??"
            elif "?" in subject:
                category = "?"
            else:
                category = "敺偷??
            results.append({"category": category, "sender": sender, "subject": subject})

        page_idx += 1
        if new_matches:
            print(f"    蝚?{page_idx} ???啣? {len(new_matches)} 撠?蝝航? {len(results)}嚗?, flush=True)
            no_new = 0
        else:
            no_new += 1

        # ?脣?????scrollTop
        cur_scroll_top = page.evaluate(f"""(() => {{
            const el = (() => {{ {_largest_scroller_js()} }})();
            return el.scrollTop;
        }})()""")

        # scrollTop 瘝? ?????啣?鈭?
        if cur_scroll_top == prev_scroll_top:
            print(f"    scrollTop ?芰宏??蝣箄??啣?嚗 {page_idx} ??{len(results)} 撠泵??", flush=True)
            break

        prev_scroll_top = cur_scroll_top

        # 撌脣摨??汗?典??梧?????銝甈⊥?敺???蝯?
        if _scroller_at_bottom(page):
            print(f"    撌脫?啣?嚗 {page_idx} ??{len(results)} 撠泵??", flush=True)
            break

        # ??? N ??唬蜓?剁?靽?剁??虜銝?閫貊嚗?
        if no_new >= no_new_limit:
            print(f"    ??? {no_new_limit} ??唬蜓?剁??迫嚗 {len(results)} 撠?", flush=True)
            break

        _scroll_down_page(page)
        page.wait_for_timeout(1200)

    return results


def phase1_scan_and_move():
    """
    ???嗡辣?????靽∩辣嚗蒂撠?蝪賣????宏??Unsigned 鞈?憭整?
    ?孵? #6嚗????港遢皜??蝘餃?嚗宏?????啣?雿?蝝?銝?頛????
    ?孵? #5嚗??key ??(sender, subject)嚗?銝餅銝?鈭箔??◤頝喲???
    ? pending list嚗{category, sender, subject}, ...]
    """
    print("\n????Phase 1嚗??隞嗅 & 蝘餃 Unsigned ????, flush=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        _login(page)

        # ?? 蝚砌?甇伐????脣???摰皜嚗1.3.2嚗摰?virtual scrolling嚗??????
        scanned = _scroll_and_collect_all(page)
        results.extend(scanned)

        print(f"  ????{len(results)} 撠泵??隞嗥?靽∩辣", flush=True)

        # ?? 蝚砌?甇伐???蝘餃 Unsigned嚗irtual scroll ?澆捆嚗?啣??曆??唳?隤撓嚗????
        for mail in results:
            sender, subject, category = mail["sender"], mail["subject"], mail["category"]

            # 蝘餃??航霈?DOM ?? ???岫?嗅? DOM嚗銝???????
            item = _locate_inbox_item(page, sender, subject)
            if item is None:
                item = _find_item_by_scroll(page, sender, subject)

            if item is None:
                mail["move_status"] = "not_found"
                print(f"  [{category}] {sender} ??{subject} ?????曆???, flush=True)
                continue

            try:
                move_status = _move_email_to_folder(page, item, "Unsigned")
            except Exception as e:
                print(f"  [{category}] {sender} ??{subject} ????蝘餃?憭望?嚗e}", flush=True)
                mail["move_status"] = "error"
                _scroll_to_top(page)
                continue

            mail["move_status"] = move_status
            icon = "?? if move_status == "moved" else "??
            print(f"  [{category}] {sender} ??{subject} ??{icon} Unsigned", flush=True)
            page.wait_for_timeout(800)

        browser.close()

    pending_count = sum(1 for r in results if r['category'] == '敺偷??)
    notif_count   = sum(1 for r in results if r['category'] == '?')
    done_count    = sum(1 for r in results if r['category'] == '?詨??')
    print(f"  摰?嚗pending_count} 蝑?蝪賣?notif_count} 蝑撌脩宏??Unsigned嚗done_count} 蝑?", flush=True)
    return results


# ????????????????????????????????????????????????????????????????????????????????
# Phase 2 ??Android ?詨?嚗?閮策 hcl_approve_android.py嚗?
# ????????????????????????????????????????????????????????????????????????????????

def phase2_approve(pending_items, ai_judge_fn=None, check_leftover=False, review_only=False):
    from hcl_approve_android import phase2_approve_android
    return phase2_approve_android(pending_items, ai_judge_fn=ai_judge_fn,
                                  check_leftover=check_leftover, review_only=review_only)


# ????????????????????????????????????????????????????????????????????????????????
# Phase 3 ??蝘餃???Sign嚗laywright嚗?
# ????????????????????????????????????????????????????????????????????????????????

def _find_email_in_folder(page, sender, subject, folder="Unsigned"):
    """?冽?摰??冗銝剜?啣??縑隞嗅?蝝?""
    keywords = ["憭??, "??唾?", "?芸?∪", "憭?", "憭?桅"]
    search_kw = next((kw for kw in keywords if kw in subject), sender)
    candidates = page.locator(f'[role="treeitem"]:has-text("{search_kw}")').all()
    for item in candidates:
        text = item.inner_text()
        if sender in text and any(part in text for part in subject.split("嚗?)[:1]):
            return item
    return candidates[0] if candidates else None


def phase3_move_to_sign(pending_items):
    """撠?Unsigned 銝剖歇???縑隞嗥宏??Sign 鞈?憭?""
    print("\n????Phase 3嚗宏??Unsigned ??Sign ????, flush=True)
    if not pending_items:
        print("  瘝?靽∩辣?閬宏??, flush=True)
        return []

    # ?芰宏??蝪賣?嚗??急?嚗?
    to_move = [x for x in pending_items if x.get("category") in ("敺偷??, "?", "?詨??")]
    if not to_move:
        print("  瘝?靽∩辣?閬宏??, flush=True)
        return []

    move_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        _login(page)

        # 撠??Unsigned 鞈?憭?
        # 暺椰?渲??冗璅寞??Unsigned
        try:
            page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
        except Exception:
            print("  霅血?嚗瘜?仿???Unsigned嚗?閰阡? URL 撠", flush=True)

        _scroll_to_bottom(page)

        for i, mail in enumerate(to_move, 1):
            sender  = mail["sender"]
            subject = mail["subject"]
            print(f"  [{i}/{len(to_move)}] {sender} ??{subject}", flush=True)

            if i > 1:
                # ?頛 Unsigned 鞈?憭?
                try:
                    page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                _scroll_to_bottom(page)

            item_el = _find_email_in_folder(page, sender, subject)
            if item_el is None:
                print(f"    ???曆??堆??航撌脩宏??", flush=True)
                move_results.append({"sender": sender, "subject": subject, "status": "not_found"})
                continue

            status = _move_email_to_folder(page, item_el, "Sign")
            icon = "?? if status == "moved" else "??
            print(f"    ??{icon} {status}", flush=True)
            move_results.append({"sender": sender, "subject": subject, "status": status})

        browser.close()

    return move_results


# ????????????????????????????????????????????????????????????????????????????????
# 銝餌?撘?
# ????????????????????????????????????????????????????????????????????????????????

def main():
    # --review嚗祟?交芋撘?Phase 2 ?芣???詨?嚗縑隞嗥???Unsigned嚗??#8嚗?
    #   瘚?嚗? --review ??Claude 霈?芸?撖拇 ??蝣箄?敺?頝?甈∴?銝葆 flag嚗?
    #   甇斗? Phase 1 ? 0 蝑???leftover 瑼Ｘ?交??詨? Unsigned 銝剔?靽∩辣??
    review_only = "--review" in sys.argv

    print("?? HCL Notes 蝪賣?芸???憪?Android ??"
          + ("?祟?交芋撘??芣???詨??? if review_only else ""), flush=True)

    # Phase 1嚗??蒂蝘餃 Unsigned
    pending_items = phase1_scan_and_move()
    if not pending_items:
        # ?嗡辣????靽∩辣嚗? Unsigned ?航??甈⊥摰???縑隞塚??孵? #1嚗?
        print("\n?嗡辣?????偷?訾縑隞塚?瑼Ｘ Unsigned ?臬??縑隞?..", flush=True)

    with open("/tmp/hcl_scan_results.json", "w") as f:
        json.dump({"pending": pending_items}, f, ensure_ascii=False, indent=2)

    # Phase 2嚗ndroid ?詨?
    # ai_judge_fn ??勗?怎垢嚗laude skill嚗?交??瑕撘?
    # ?湔?瑁???閮剔 None嚗?霈?底蝝啣摰對??湔?詨?嚗?
    approve_results = phase2_approve(pending_items, ai_judge_fn=None,
                                     check_leftover=not pending_items,
                                     review_only=review_only)

    if not pending_items and not approve_results:
        print("\n?嗡辣??? Unsigned ?賣??????偷?訾縑隞嗚?)
        return

    with open("/tmp/hcl_approve_results.json", "w") as f:
        json.dump({"total": len(approve_results), "results": approve_results},
                  f, ensure_ascii=False, indent=2)

    # Phase 3嚗蝘餃?撌脫??撌脰???靽∩辣嚗??????Unsigned
    DONE_STATUSES = {"approved", "already_approved", "notification", "approved_notification"}
    processed_subjects = {r["subject"] for r in approve_results if r.get("status") in DONE_STATUSES}
    pending_subjects   = {item["subject"] for item in pending_items}

    # Phase 1 ?銝?Phase 2 撌脰???
    items_to_move = [item for item in pending_items if item["subject"] in processed_subjects]

    # Phase 2 ???唬?銝 Phase 1 皜???撠勗 Unsigned ??靽∩辣嚗?
    for r in approve_results:
        if r.get("status") in DONE_STATUSES and r["subject"] not in pending_subjects:
            items_to_move.append({"sender": r.get("sender", ""), "subject": r["subject"], "category": "敺偷??})

    unprocessed = [item for item in pending_items if item["subject"] not in processed_subjects]
    if unprocessed:
        print(f"\n  ??  {len(unprocessed)} 蝑?詨?嚗?? Unsigned嚗?, flush=True)
        for item in unprocessed:
            print(f"    - {item['sender']} ??{item['subject']}", flush=True)
    move_results = phase3_move_to_sign(items_to_move)

    final = {
        "scan_total":  len(pending_items),
        "approve":     approve_results,
        "move":        move_results,
    }
    with open("/tmp/hcl_process_results.json", "w") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    print("\n???券摰?嚗?, flush=True)
    print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

