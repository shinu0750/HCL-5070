#!/usr/bin/env python3
"""
HCL Notes 簽核自動化 — Playwright phases (1 & 3)

Phase 1: 掃描收件匣，符合 APPROVAL_KEYWORDS 的信件全移到 Unsigned（不分類）
Phase 3: Unsigned 中已完成的信件移到 Sign

用法：
  python hcl_process_all.py --phase1
  python hcl_process_all.py --phase3
"""

import os, json, re, sys, tempfile
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

_env_path = os.path.expanduser(os.environ.get("HCL_ENV_FILE", "~/.hermes/.env"))
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
_TMP        = tempfile.gettempdir()

# 與 hcl_approve_android.py 保持一致
APPROVAL_KEYWORDS = ["外出單", "加班申請", "未刷卡單", "外出單通知"]


# ════════════════════════════════════════════════════════════════════════════════
# 共用 Playwright 工具
# ════════════════════════════════════════════════════════════════════════════════

def _login(page):
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


def _largest_scroller_js():
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
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_largest_scroller_js()} }})();
        el.scrollTop = el.scrollTop + el.clientHeight * {ratio};
        return el.scrollTop;
    }})()""")


def _scroller_at_bottom(page):
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_largest_scroller_js()} }})();
        return el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
    }})()""")


def _scroll_to_bottom(page):
    _scroll_to_top(page)
    for _ in range(60):
        _scroll_down_page(page)
        page.wait_for_timeout(700)
        if _scroller_at_bottom(page):
            break


def _move_email_to_folder(page, item, folder_name):
    """將信件移到指定資料夾。回傳 'moved' 或 error string。"""
    item.click()
    try:
        page.wait_for_selector("div.action-tray-populated", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(500)

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
    folder_input.fill("")
    folder_input.type(folder_name, delay=50)
    page.wait_for_timeout(1000)

    sign_item = page.locator(
        f"div.folder-tray-float.show [role='treeitem']:visible:has-text('{folder_name}')"
    ).first
    try:
        sign_item.wait_for(state="visible", timeout=5000)
        sign_item.click()
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


def _flatten(s):
    """移除所有空白字元（含 Playwright innerText 在窄欄位軟換行插入的 \\n），
    避免因視覺換行位置不同造成 substring 比對失敗。"""
    return re.sub(r"\s+", "", s)


def _locate_item(page, subject, sender=""):
    """在目前列表中找到指定信件元素。"""
    kw = next((k for k in APPROVAL_KEYWORDS if k in subject), None)
    sel = f'[role="treeitem"]:has-text("{kw}")' if kw else '[role="treeitem"]'
    flat_subject = _flatten(subject)
    for item in page.locator(sel).all():
        try:
            text = item.inner_text(timeout=3000)
        except Exception:
            continue
        if flat_subject in _flatten(text) and (not sender or sender in text):
            return item
    return None


def _find_item_by_scroll(page, subject, sender="", max_pages=80):
    """從頂部逐頁捲動找指定信件。"""
    _scroll_to_top(page)
    page.wait_for_timeout(500)
    for _ in range(max_pages):
        item = _locate_item(page, subject, sender)
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
    """從 treeitem inner_text 解析 (sender, subject)。"""
    DATE_LABELS = {"寄件者", "主旨", "訊息摘要", "今天", "昨天", "本週", "上週", "更早"}
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return None, None
    subject = next((l for l in lines if any(k in l for k in APPROVAL_KEYWORDS)), lines[0])
    # len(l) > 1：排除軟換行把姓名切斷後留下的單字元殘片（例如 "Tzu-Sheng Yang" 被
    # 硬生生從 "T" 後面斷行，導致 "T" 被誤判為完整寄件者名稱）
    sender  = next((l for l in lines if len(l) > 1 and l not in DATE_LABELS
                    and not any(k in l for k in APPROVAL_KEYWORDS)), "")
    return sender, subject


def _scan_visible_matches(page, seen):
    """掃描當前 DOM 中符合關鍵字的信件，回傳新發現的 [(sender, subject), ...]。"""
    new_matches = []
    for item in page.locator('[role="treeitem"]').all():
        try:
            text = item.inner_text(timeout=2000).strip()
        except Exception:
            continue
        sender, subject = _parse_treeitem(text)
        if not subject or not any(k in subject for k in APPROVAL_KEYWORDS):
            continue
        key = (sender, subject)
        if key in seen:
            continue
        seen.add(key)
        new_matches.append((sender, subject))
    return new_matches


def _scroll_and_collect_all(page, no_new_limit=50):
    """逐頁捲動收集所有符合關鍵字的信件。回傳 [{sender, subject}]。"""
    print("  從頂部逐頁掃描（兼容 virtual scrolling）...", flush=True)
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
            results.append({"sender": sender, "subject": subject})

        page_idx += 1
        if new_matches:
            print(f"    第 {page_idx} 頁：新增 {len(new_matches)} 封（累計 {len(results)}）", flush=True)
            no_new = 0
        else:
            no_new += 1

        cur_scroll_top = page.evaluate(f"""(() => {{
            const el = (() => {{ {_largest_scroller_js()} }})();
            return el.scrollTop;
        }})()""")

        if cur_scroll_top == prev_scroll_top:
            print(f"    scrollTop 未移動，確認到底（{len(results)} 封符合）", flush=True)
            break

        prev_scroll_top = cur_scroll_top

        if _scroller_at_bottom(page):
            print(f"    已捲到底（{len(results)} 封符合）", flush=True)
            break

        if no_new >= no_new_limit:
            print(f"    連續 {no_new_limit} 頁無新主旨，停止", flush=True)
            break

        _scroll_down_page(page)
        page.wait_for_timeout(1200)

    return results


# ════════════════════════════════════════════════════════════════════════════════
# Phase 1 — 掃描收件匣並移到 Unsigned
# ════════════════════════════════════════════════════════════════════════════════

def phase1_scan_and_move():
    """
    掃描收件匣，將所有符合 APPROVAL_KEYWORDS 的信件移到 Unsigned。
    不做分類，由 Phase 2 Android 端處理。
    回傳 [{sender, subject}]，同時寫入 hcl_scan_results.json。
    """
    print("\n═══ Phase 1：掃描收件匣 & 移到 Unsigned ═══", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        _login(page)

        results = _scroll_and_collect_all(page)
        print(f"  掃描到 {len(results)} 封符合條件的信件", flush=True)

        for mail in results:
            sender, subject = mail["sender"], mail["subject"]

            item = _locate_item(page, subject, sender)
            if item is None:
                item = _find_item_by_scroll(page, subject, sender)

            if item is None:
                mail["move_status"] = "not_found"
                print(f"  {sender} — {subject} → ✗ 找不到", flush=True)
                continue

            try:
                status = _move_email_to_folder(page, item, "Unsigned")
            except Exception as e:
                mail["move_status"] = "error"
                print(f"  {sender} — {subject} → ✗ 移動失敗：{e}", flush=True)
                _scroll_to_top(page)
                continue

            mail["move_status"] = status
            icon = "✓" if status == "moved" else "✗"
            print(f"  {sender} — {subject} → {icon} Unsigned", flush=True)
            page.wait_for_timeout(800)

        browser.close()

    print(f"  完成：{len(results)} 封移到 Unsigned", flush=True)

    with open(os.path.join(_TMP, "hcl_scan_results.json"), "w") as f:
        json.dump({"emails": results}, f, ensure_ascii=False, indent=2)

    return results


# ════════════════════════════════════════════════════════════════════════════════
# Phase 3 — 將已完成的信件從 Unsigned 移到 Sign
# ════════════════════════════════════════════════════════════════════════════════

def _find_email_in_unsigned(page, subject, sender=""):
    """在 Unsigned 資料夾中找到指定信件元素。"""
    kw = next((k for k in APPROVAL_KEYWORDS if k in subject), None)
    candidates = page.locator(
        f'[role="treeitem"]:has-text("{kw}")' if kw else '[role="treeitem"]'
    ).all()
    for item in candidates:
        try:
            text = item.inner_text(timeout=3000)
        except Exception:
            continue
        subj_part = subject.split("，")[0].strip()
        if subj_part and _flatten(subj_part) in _flatten(text):
            if not sender or sender in text:
                return item
    return None


def phase3_move_to_sign(done_subjects):
    """
    將 Unsigned 中已完成的信件移到 Sign。
    done_subjects: set of subject strings（approved/notification 狀態）
    """
    print("\n═══ Phase 3：移動 Unsigned → Sign ═══", flush=True)
    if not done_subjects:
        print("  沒有信件需要移動", flush=True)
        return []

    # 從 scan results 取 sender 資訊
    sender_map = {}
    scan_path = os.path.join(_TMP, "hcl_scan_results.json")
    if os.path.exists(scan_path):
        with open(scan_path) as f:
            for email in json.load(f).get("emails", []):
                sender_map[email["subject"]] = email.get("sender", "")

    to_move = [{"subject": s, "sender": sender_map.get(s, "")} for s in done_subjects]
    move_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        _login(page)

        try:
            page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
        except Exception:
            print("  警告：無法直接點選 Unsigned", flush=True)

        _scroll_to_bottom(page)

        for i, mail in enumerate(to_move, 1):
            subject = mail["subject"]
            sender  = mail["sender"]
            print(f"  [{i}/{len(to_move)}] {sender} — {subject}", flush=True)

            if i > 1:
                try:
                    page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                _scroll_to_bottom(page)

            item_el = _find_email_in_unsigned(page, subject, sender)
            if item_el is None:
                print(f"    → 找不到（可能已移動）", flush=True)
                move_results.append({"subject": subject, "status": "not_found"})
                continue

            status = _move_email_to_folder(page, item_el, "Sign")
            icon = "✓" if status == "moved" else "✗"
            print(f"    → {icon} {status}", flush=True)
            move_results.append({"subject": subject, "status": status})

        browser.close()

    return move_results


# ════════════════════════════════════════════════════════════════════════════════
# 主程式
# ════════════════════════════════════════════════════════════════════════════════

def main():
    if "--phase1" in sys.argv:
        phase1_scan_and_move()

    elif "--phase3" in sys.argv:
        approve_path = os.path.join(_TMP, "hcl_approve_results.json")
        if not os.path.exists(approve_path):
            print("  找不到 hcl_approve_results.json，無法執行 Phase 3", flush=True)
            return

        with open(approve_path) as f:
            data = json.load(f)

        DONE_STATUSES = {"approved", "already_approved", "notification", "approved_notification"}
        done_subjects = {r["subject"] for r in data.get("results", [])
                         if r.get("status") in DONE_STATUSES}
        failed_subjects = {r["subject"] for r in data.get("results", [])
                           if r.get("status") not in DONE_STATUSES}

        if failed_subjects:
            print(f"\n  ⚠️ {len(failed_subjects)} 筆未完成，保留在 Unsigned：", flush=True)
            for s in failed_subjects:
                print(f"    - {s}", flush=True)

        move_results = phase3_move_to_sign(done_subjects)
        with open(os.path.join(_TMP, "hcl_move_results.json"), "w") as f:
            json.dump(move_results, f, ensure_ascii=False, indent=2)

    else:
        print("用法：hcl_process_all.py --phase1 | --phase3", flush=True)


if __name__ == "__main__":
    main()
