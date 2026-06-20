#!/usr/bin/env python3
"""
掃收件匣找出符合關鍵字的信件（含討論串），逐一移到「05Other」資料匣。
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
    page.click('button[type="submit"], input[type="submit"], button:has-text("登入")')
    page.wait_for_load_state("networkidle")
    page.goto(VERSE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('[role="treeitem"]', timeout=15000)
    page.wait_for_timeout(2000)


# ── Virtual-scroll 兼容的捲動工具（v1.2.0）─────────────────────────────────
# Verse 收件匣是 virtual scroll：DOM 只保留可見窗 ± buffer，捲過的會被回收。
# 舊版「跳到底再 locator.all()」只能看到當下渲染的 ~30 封，120 封 inbox 會漏大半。
# 改為「從頂部逐頁捲動，每頁掃 DOM 累積去重，到底或連續 N 頁無新主旨才停」。

_LARGEST_SCROLLER_JS = """
    const els = [...document.querySelectorAll('*')].filter(el =>
        el.scrollHeight - el.clientHeight > 50 &&
        getComputedStyle(el).overflowY !== 'visible'
    );
    els.sort((a, b) => b.scrollHeight - a.scrollHeight);
    return els[0] || document.scrollingElement;
"""


def scroll_to_top(page):
    """捲回列表頂部，確保點擊最新信件時 click 能正確觸發閱讀窗格"""
    page.evaluate(f"(() => {{ const el = (() => {{ {_LARGEST_SCROLLER_JS} }})(); el.scrollTop = 0; }})()")
    page.wait_for_timeout(800)


def _scroll_down_page(page, ratio=0.85):
    """往下捲約一頁高度（留 15% 重疊避免漏掉邊界信件）"""
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
    """逐頁捲到底（兼容 virtual scroll）— 給移動階段重整 DOM 用。"""
    scroll_to_top(page)
    for _ in range(60):
        _scroll_down_page(page)
        page.wait_for_timeout(700)
        if _scroller_at_bottom(page):
            break


def _scan_visible_matches(page, seen):
    """掃當前 DOM，回傳新發現的 [{sender, subject, keyword, folder}, ...]，已見過的跳過。"""
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
    從頂部逐頁捲動，每頁掃描 DOM 累積符合關鍵字的信件。
    停止條件：捲到底（_scroller_at_bottom）OR 達 max_pages 上限。
    v1.2.1：移除「連續 N 頁無新主旨就停」的早停邏輯 — 符合主旨可能稀疏分布在
    inbox 中段或尾段，前幾頁沒命中不代表後面沒有，必須真的捲到底才能下結論。
    """
    scroll_to_top(page)
    page.wait_for_timeout(1000)

    seen = set()
    items = []
    for page_idx in range(1, max_pages + 1):
        new_items = _scan_visible_matches(page, seen)
        items.extend(new_items)
        if new_items:
            print(f"  [scan] 第 {page_idx} 頁：新增 {len(new_items)} 封（累計 {len(items)}）")

        if _scroller_at_bottom(page):
            print(f"  [scan] 已捲到底（共 {page_idx} 頁，{len(items)} 封符合）")
            break

        _scroll_down_page(page)
        page.wait_for_timeout(700)
    else:
        print(f"  [scan] 達上限 {max_pages} 頁仍未到底（已收 {len(items)} 封）")

    return items


def _scan_for_match(page, short, keyword):
    """掃當前 DOM 找符合 (short, keyword) 的 treeitem。"""
    for item in page.locator(f'[role="treeitem"]:has-text("{keyword}")').all():
        try:
            if short in item.inner_text(timeout=2000):
                return item
        except Exception:
            continue
    return None


def find_item_in_inbox(page, subject, keyword, max_pages=80):
    """
    v1.2.0：從頂部逐頁捲動找信件（兼容 virtual scroll）。
    舊版只看當下 DOM 一頁，120 封 inbox 中靠後的信件會找不到。
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
    """點擊信件 → 點資料夾 icon → 輸入目標資料夾 → Enter"""
    item.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    item.click()

    # 用完整路徑避免選到隱藏的同名 button
    # 單封信 DOM：div.sticky-header > div.action-bar.collapse-stage-0.action-tray-populated > button
    # 討論串 DOM：div.sticky-header > div > button.action.pim-move-to-folder.icon （沒有 action-bar class）
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
        print(f"  ✗ 找不到資料夾 icon：{sender} / {subject[:40]}")
        return "error_no_button"
    move_btn.click()
    page.wait_for_timeout(500)

    try:
        page.wait_for_selector("div.folder-tray-float.show", timeout=8000)
    except Exception:
        print(f"  ✗ 找不到資料夾 popup：{sender} / {subject[:40]}")
        return "error_no_popup"

    folder_input = page.locator("div.folder-tray-float.show input.folder-search-input")
    folder_input.click()
    # v1.2.2：先 fill 清空 + 再 type 觸發 React onChange，delay=50 避免 IME 殘留
    folder_input.fill("")
    folder_input.type(folder, delay=50)
    page.wait_for_timeout(1000)

    # v1.2.2：明確比對資料夾名稱 — 舊版用 .first，過濾沒生效時會選錯項目
    # 導致回報 moved 但實際沒移動（中文名稱資料夾尤其常見）
    folder_item = page.locator(
        f"div.folder-tray-float.show [role='treeitem']:visible:has-text('{folder}')"
    ).first
    try:
        folder_item.wait_for(state="visible", timeout=5000)
        folder_item.click()
        page.wait_for_timeout(1500)
    except Exception:
        print(f"  ⚠️ 找不到含「{folder}」的項目，改按 Enter")
        folder_input.press("Enter")
        page.wait_for_timeout(1500)

    # v1.2.2：popup 沒關 = 移動失敗
    if page.locator("div.folder-tray-float.show").count() > 0:
        print(f"  ⚠️ 資料夾 popup 未關閉，補按 Enter：{sender} / {subject[:40]}")
        try:
            folder_input.press("Enter")
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if page.locator("div.folder-tray-float.show").count() > 0:
            return "error_popup_stuck"

    print(f"  ✓ 已移動：{sender} / {subject[:50]}")
    return "moved"


def main():
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        login(page)

        # v1.2.0：collect_subjects 內部會從頂部逐頁捲動掃描，不需先 scroll_to_bottom
        mails = collect_subjects(page)
        scroll_to_top(page)
        kw_str = "、".join(KEYWORDS)
        print(f"找到 {len(mails)} 封符合關鍵字（{kw_str}）的信件，開始移動到「{TARGET_FOLDER}」...\n")

        for i, mail in enumerate(mails, 1):
            sender  = mail["sender"]
            subject = mail["subject"]
            print(f"[{i}/{len(mails)}] {sender} - {subject[:50]}")

            # 每次移動前回到收件匣，find_item_in_inbox 會自行從頂部捲到目標位置
            if i > 1:
                page.goto(VERSE_URL)
                page.wait_for_load_state("networkidle")
                page.wait_for_selector('[role="treeitem"]', timeout=15000)
                page.wait_for_timeout(1500)

            item_el = find_item_in_inbox(page, subject, mail["keyword"])
            if item_el is None:
                print(f"  ✗ 找不到信件（可能已移動）：{subject[:50]}")
                results.append({**mail, "status": "not_found"})
                continue

            status = move_to_folder(page, item_el, sender, subject, mail["folder"])
            results.append({**mail, "status": status})

        browser.close()

    moved   = sum(1 for r in results if r["status"] == "moved")
    errors  = sum(1 for r in results if r["status"] != "moved")
    print(f"\n完成：{moved} 封已移動，{errors} 封失敗")

    output = {"total": len(mails), "moved": moved, "results": results}
    with open("/tmp/hcl_move_construction.json", "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
