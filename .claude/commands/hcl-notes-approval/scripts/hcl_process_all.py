#!/usr/bin/env python3
"""
HCL Notes 簽核自動化 — 完整流程（Android 版）

  Phase 1 (Playwright) : 掃描 HCL Verse 收件匣，找出待簽核信件，移到 Unsigned 資料夾
  Phase 2 (Android)    : 透過 ADB 操作 Android 模擬器，逐一開啟 Nomad 表單並核准
  Phase 3 (Playwright) : 將 Unsigned 中已處理的信件移到 Sign 資料夾
"""

# ── 環境變數載入 ─────────────────────────────────────────────────────────────
import os, tempfile, json, re, subprocess, sys, time
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

PORTAL_URL = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL   = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME    = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD    = os.environ.get("HCL_PASSWORD",    "")

APPROVAL_KEYWORDS = ["外出單", "加班申請", "未刷卡單", "外出通知", "請假單"]


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
    """JS：找出收件匣的可捲動容器（最大的 overflow-y!=visible 元素）。"""
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
    """往下捲約一頁高度（留 15% 重疊避免漏掉邊界信件）。回傳新的 scrollTop。"""
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_largest_scroller_js()} }})();
        el.scrollTop = el.scrollTop + el.clientHeight * {ratio};
        return el.scrollTop;
    }})()""")


def _scroller_at_bottom(page):
    """JS：判斷收件匣捲動條是否已到底（容差 5px）。"""
    return page.evaluate(f"""(() => {{
        const el = (() => {{ {_largest_scroller_js()} }})();
        return el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
    }})()""")


# 保留舊名稱供其他呼叫者相容（_reload_inbox），但改為「捲完所有頁」而非單純跳到底
def _scroll_to_bottom(page):
    """逐頁往下捲到底（兼容 virtual scroll，舊呼叫者用）。"""
    _scroll_to_top(page)
    for _ in range(60):
        _scroll_down_page(page)
        page.wait_for_timeout(700)
        if _scroller_at_bottom(page):
            break


# ════════════════════════════════════════════════════════════════════════════════
# Phase 1 — 掃描收件匣並移到 Unsigned（Playwright）
# ════════════════════════════════════════════════════════════════════════════════

def _move_email_to_folder(page, item, folder_name):
    """將信件移到指定資料夾（與 Phase 3 相同機制）"""
    item.click()
    try:
        page.wait_for_selector("div.action-tray-populated", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    # 找移動按鈕（不要求 collapse-stage-0，該 class 並非所有信件都有）
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
    # v1.3.4：先 fill 清空 + 再 type 觸發 React onChange，delay=50 避免 IME 殘留
    folder_input.fill("")
    folder_input.type(folder_name, delay=50)
    page.wait_for_timeout(1000)

    # v1.3.4：明確比對資料夾名稱 — 舊版用 .first 在 Chinese folder name 時會選錯項目
    # 導致回報 moved 但實際沒移動（meeting/construction 已實測中這個 bug）
    sign_item = page.locator(
        f"div.folder-tray-float.show [role='treeitem']:visible:has-text('{folder_name}')"
    ).first
    try:
        sign_item.wait_for(state="visible", timeout=5000)
        sign_item.click()
    except Exception:
        folder_input.press("Enter")
    page.wait_for_timeout(1500)

    # v1.3.4：popup 沒關 = 移動失敗
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
    """重新載入收件匣並捲到底部"""
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=15000)
    page.wait_for_timeout(1500)
    _scroll_to_bottom(page)


def _locate_inbox_item(page, sender, subject):
    """在目前列表中重新定位指定信件元素（移動後 DOM 會更新，需重新定位）"""
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
    Verse 用 virtual scrolling，DOM 只保留可見窗 ± buffer，捲過的會被回收。
    本函式從頂部開始逐頁往下捲，每頁檢查 (sender, subject) 是否在當前 DOM 中。
    回傳找到的 Locator，找不到回傳 None。
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
    """從 treeitem inner_text 解析 (sender, subject)。回傳 (sender, subject) 或 (None, None)。"""
    DATE_LABELS = {"寄件者", "主旨", "訊息摘要", "今天", "昨天", "本週", "上週", "更早"}
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
    掃描當前 DOM 中所有 [role="treeitem"]，回傳新發現的 [(sender, subject), ...]。
    已在 seen set 中的會跳過；seen 會就地更新。
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
        # 只收主旨含關鍵字的
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
    從頂部開始逐頁捲動，每頁掃描可見信件並累積符合關鍵字者。
    停止條件：scrollTop 確實沒有移動（真正到底）。
    no_new_limit 保留為保險用（預設 50，對應 ~120 封收件匣）。
    回傳 [{sender, subject, category}, ...]
    """
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
            if "已核准" in subject or "已批准" in subject:
                category = "核准通知"
            elif "通知" in subject:
                category = "通知"
            else:
                category = "待簽核"
            results.append({"category": category, "sender": sender, "subject": subject})

        page_idx += 1
        if new_matches:
            print(f"    第 {page_idx} 頁：新增 {len(new_matches)} 封（累計 {len(results)}）", flush=True)
            no_new = 0
        else:
            no_new += 1

        # 捲動前記錄 scrollTop
        cur_scroll_top = page.evaluate(f"""(() => {{
            const el = (() => {{ {_largest_scroller_js()} }})();
            return el.scrollTop;
        }})()""")

        # scrollTop 沒變 → 真的到底了
        if cur_scroll_top == prev_scroll_top:
            print(f"    scrollTop 未移動，確認到底（共 {page_idx} 頁，{len(results)} 封符合）", flush=True)
            break

        prev_scroll_top = cur_scroll_top

        # 已到底（瀏覽器回報）→ 再掃一次最後一頁後結束
        if _scroller_at_bottom(page):
            print(f"    已捲到底（共 {page_idx} 頁，{len(results)} 封符合）", flush=True)
            break

        # 連續 N 頁無新主旨（保險用，通常不會觸發）
        if no_new >= no_new_limit:
            print(f"    連續 {no_new_limit} 頁無新主旨，停止（共 {len(results)} 封）", flush=True)
            break

        _scroll_down_page(page)
        page.wait_for_timeout(1200)

    return results


def phase1_scan_and_move():
    """
    掃描收件匣，分類信件，並將「待簽核」與「通知」移到 Unsigned 資料夾。
    改善 #6：先掃完整份清單再逐一移動（移動後僅重新定位元素，不重載整頁）。
    改善 #5：去重 key 用 (sender, subject)，同主旨不同人不會被跳過。
    回傳 pending list：[{category, sender, subject}, ...]
    """
    print("\n═══ Phase 1：掃描收件匣 & 移到 Unsigned ═══", flush=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        _login(page)

        # ── 第一步：逐頁捲動掃描完整清單（v1.3.2：兼容 virtual scrolling）──────
        scanned = _scroll_and_collect_all(page)
        results.extend(scanned)

        print(f"  掃描到 {len(results)} 封符合條件的信件", flush=True)

        # ── 第二步：逐一移到 Unsigned（virtual scroll 兼容：捲到底找不到才認輸）────
        for mail in results:
            sender, subject, category = mail["sender"], mail["subject"], mail["category"]

            # 移動可能讓 DOM 重排 → 先試當前 DOM，找不到再從頂部逐頁找
            item = _locate_inbox_item(page, sender, subject)
            if item is None:
                item = _find_item_by_scroll(page, sender, subject)

            if item is None:
                mail["move_status"] = "not_found"
                print(f"  [{category}] {sender} — {subject} → ✗ 找不到", flush=True)
                continue

            try:
                move_status = _move_email_to_folder(page, item, "Unsigned")
            except Exception as e:
                print(f"  [{category}] {sender} — {subject} → ✗ 移動失敗：{e}", flush=True)
                mail["move_status"] = "error"
                _scroll_to_top(page)
                continue

            mail["move_status"] = move_status
            icon = "✓" if move_status == "moved" else "✗"
            print(f"  [{category}] {sender} — {subject} → {icon} Unsigned", flush=True)
            page.wait_for_timeout(800)

        browser.close()

    pending_count = sum(1 for r in results if r['category'] == '待簽核')
    notif_count   = sum(1 for r in results if r['category'] == '通知')
    done_count    = sum(1 for r in results if r['category'] == '核准通知')
    print(f"  完成：{pending_count} 筆待簽核、{notif_count} 筆通知已移到 Unsigned，{done_count} 筆核准通知", flush=True)
    return results


# ════════════════════════════════════════════════════════════════════════════════
# Phase 2 — Android 核准（委託給 hcl_approve_android.py）
# ════════════════════════════════════════════════════════════════════════════════

def phase2_approve(pending_items, ai_judge_fn=None, check_leftover=False, review_only=False):
    from hcl_approve_android import phase2_approve_android
    return phase2_approve_android(pending_items, ai_judge_fn=ai_judge_fn,
                                  check_leftover=check_leftover, review_only=review_only)


# ════════════════════════════════════════════════════════════════════════════════
# Phase 3 — 移動到 Sign（Playwright）
# ════════════════════════════════════════════════════════════════════════════════

def _find_email_in_folder(page, sender, subject, folder="Unsigned"):
    """在指定資料夾中找到對應信件元素"""
    keywords = ["外出單", "加班申請", "未刷卡單", "外出通知", "外出單通知"]
    search_kw = next((kw for kw in keywords if kw in subject), sender)
    candidates = page.locator(f'[role="treeitem"]:has-text("{search_kw}")').all()
    for item in candidates:
        text = item.inner_text()
        if sender in text and any(part in text for part in subject.split("，")[:1]):
            return item
    return candidates[0] if candidates else None


def phase3_move_to_sign(pending_items):
    """將 Unsigned 中已處理的信件移到 Sign 資料夾"""
    print("\n═══ Phase 3：移動 Unsigned → Sign ═══", flush=True)
    if not pending_items:
        print("  沒有信件需要移動", flush=True)
        return []

    # 只移動待簽核和通知（不含核准通知）
    to_move = [x for x in pending_items if x.get("category") in ("待簽核", "通知", "核准通知")]
    if not to_move:
        print("  沒有信件需要移動", flush=True)
        return []

    move_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        _login(page)

        # 導航到 Unsigned 資料夾
        # 點左側資料夾樹找到 Unsigned
        try:
            page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
        except Exception:
            print("  警告：無法直接點選 Unsigned，嘗試透過 URL 導航", flush=True)

        _scroll_to_bottom(page)

        for i, mail in enumerate(to_move, 1):
            sender  = mail["sender"]
            subject = mail["subject"]
            print(f"  [{i}/{len(to_move)}] {sender} — {subject}", flush=True)

            if i > 1:
                # 重新載入 Unsigned 資料夾
                try:
                    page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                _scroll_to_bottom(page)

            item_el = _find_email_in_folder(page, sender, subject)
            if item_el is None:
                print(f"    → 找不到（可能已移動）", flush=True)
                move_results.append({"sender": sender, "subject": subject, "status": "not_found"})
                continue

            status = _move_email_to_folder(page, item_el, "Sign")
            icon = "✓" if status == "moved" else "✗"
            print(f"    → {icon} {status}", flush=True)
            move_results.append({"sender": sender, "subject": subject, "status": status})

        browser.close()

    return move_results


# ════════════════════════════════════════════════════════════════════════════════
# 主程式
# ════════════════════════════════════════════════════════════════════════════════

def main():
    # --review：審查模式，Phase 2 只截圖不核准，信件留在 Unsigned（改善 #8）
    #   流程：跑 --review → Claude 讀截圖審查 → 確認後再跑一次（不帶 flag），
    #   此時 Phase 1 掃到 0 筆，由 leftover 檢查接手核准 Unsigned 中的信件。
    review_only = "--review" in sys.argv

    print("🔄 HCL Notes 簽核自動化開始（Android 版）"
          + ("【審查模式：只截圖不核准】" if review_only else ""), flush=True)

    # Phase 1：掃描並移到 Unsigned
    pending_items = phase1_scan_and_move()
    if not pending_items:
        # 收件匣沒有新信件，但 Unsigned 可能有前次未完成的遺留信件（改善 #1）
        print("\n收件匣沒有新的簽核信件，檢查 Unsigned 是否有遺留信件...", flush=True)

    with open(os.path.join(tempfile.gettempdir(), "hcl_scan_results.json"), "w") as f:
        json.dump({"pending": pending_items}, f, ensure_ascii=False, indent=2)

    # Phase 2：Android 核准
    # ai_judge_fn 需由呼叫端（Claude skill）傳入截圖判斷函式
    # 直接執行時預設為 None（不讀取詳細內容，直接核准）
    approve_results = phase2_approve(pending_items, ai_judge_fn=None,
                                     check_leftover=not pending_items,
                                     review_only=review_only)

    if not pending_items and not approve_results:
        print("\n收件匣與 Unsigned 都沒有待處理的簽核信件。")
        return

    with open(os.path.join(tempfile.gettempdir(), "hcl_approve_results.json"), "w") as f:
        json.dump({"total": len(approve_results), "results": approve_results},
                  f, ensure_ascii=False, indent=2)

    # Phase 3：只移動已核准/已處理的信件，未處理的留在 Unsigned
    DONE_STATUSES = {"approved", "already_approved", "notification", "approved_notification"}
    processed_subjects = {r["subject"] for r in approve_results if r.get("status") in DONE_STATUSES}
    pending_subjects   = {item["subject"] for item in pending_items}

    # Phase 1 掃到且 Phase 2 已處理的
    items_to_move = [item for item in pending_items if item["subject"] in processed_subjects]

    # Phase 2 處理到但不在 Phase 1 清單的（原本就在 Unsigned 的舊信件）
    for r in approve_results:
        if r.get("status") in DONE_STATUSES and r["subject"] not in pending_subjects:
            items_to_move.append({"sender": r.get("sender", ""), "subject": r["subject"], "category": "待簽核"})

    unprocessed = [item for item in pending_items if item["subject"] not in processed_subjects]
    if unprocessed:
        print(f"\n  ⚠️  {len(unprocessed)} 筆未核准，保留在 Unsigned：", flush=True)
        for item in unprocessed:
            print(f"    - {item['sender']} — {item['subject']}", flush=True)
    move_results = phase3_move_to_sign(items_to_move)

    final = {
        "scan_total":  len(pending_items),
        "approve":     approve_results,
        "move":        move_results,
    }
    with open(os.path.join(tempfile.gettempdir(), "hcl_process_results.json"), "w") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    print("\n✅ 全部完成！", flush=True)
    print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
