#!/usr/bin/env python3
"""
HCL Notes 簽核自動化 — Phase 2 (Android)

Phase 2a (--screenshot-only):
    逐封開啟 Unsigned 信件 → 截圖 → 回到 Unsigned（不核准）
    輸出：hcl_screenshots.json

Phase 2b (--approve):
    讀取 hcl_verified.json（由 Claude skill 層寫入），核准已驗證的信件，跳過未驗證的
    輸出：hcl_approve_results.json

retry 機制由 Claude skill 層負責：
    若截圖欄位不完整，skill 層寫入 hcl_retry_subjects.json 後重跑 --screenshot-only，
    最多 3 輪，仍不完整則標記 screenshot_failed 並警告。

座標系統：橫向 rotation=1，邏輯座標 2400×1080
"""

import base64, glob, json, os, re, subprocess, sys, tempfile, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

_env_path = os.path.expanduser(os.environ.get("HCL_ENV_FILE", "~/.hermes/.env"))
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

SERIAL         = os.environ.get("HCL_ADB_SERIAL", "emulator-5554")
# 與 hcl_process_all.py 保持一致
APPROVAL_KEYWORDS = ["外出單", "加班申請", "未刷卡單", "外出單通知"]

_adb_win = r"C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
_adb_mac = "/Users/shuhsing/Library/Android/sdk/platform-tools/adb"
ADB_PATH       = _adb_win if os.path.exists(_adb_win) else _adb_mac
_TMP           = tempfile.gettempdir()
NOTES_PASSWORD = os.environ.get("HCL_NOTES_PASSWORD", "")


class PasswordError(RuntimeError):
    """HCL Notes ID 密碼錯誤或未設定時拋出。"""
    pass


class FormOpenError(RuntimeError):
    """點附件圖示/Link 都無法進入 Nomad 表單時拋出。"""
    pass


# ── 固定座標（橫向 2400×1080）──────────────────────────────────────────────────
COORD = {
    "main_mail":        (1268, 275),
    "hamburger":        (198,  115),
    "menu_folders":     (330,  846),
    "folder_unsigned":  (1326, 757),
    "attach_icon":      (415,  700),
    "comments_ok":      (1604, 753),
    "delivery_ok":      (1871, 660),
    "nomad_leave_fb":   (243,  252),
    "nomad_approve_fb": (447,  252),
}

# ── 各表單類型預設按鈕座標（y=252 固定，x 因按鈕文字寬度不同）──────────────────
FORM_BUTTONS = {
    "加班申請": {"leave": (243, 252), "approve": (447, 252)},
    "外出單":   {"leave": (243, 252), "approve": (447, 252)},
    "未刷卡":   {"leave": (289, 252), "approve": (538, 252)},
}

KEYCODE_BACK = 4


# ════════════════════════════════════════════════════════════════════════════════
# ADB 基礎工具
# ════════════════════════════════════════════════════════════════════════════════

def adb(*args, timeout=30):
    result = subprocess.run(
        [ADB_PATH, "-s", SERIAL, *args],
        capture_output=True, text=True,
        encoding='utf-8', errors='replace',
        timeout=timeout
    )
    return (result.stdout or "").strip()


def tap(x, y, delay=1.5):
    adb("shell", "input", "tap", str(x), str(y))
    time.sleep(delay)


def press_back(delay=1.5):
    adb("shell", "input", "keyevent", str(KEYCODE_BACK))
    time.sleep(delay)


def dump_ui():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    return adb("shell", "cat", "/sdcard/ui.xml")


def screenshot_to_file(path=None):
    if path is None:
        path = os.path.join(_TMP, "nomad_form.png")
    adb("shell", "screencap", "-p", "/sdcard/screen.png")
    subprocess.run([ADB_PATH, "-s", SERIAL, "pull", "/sdcard/screen.png", path],
                   capture_output=True)
    return path


def clear_stale_screenshots():
    """清除上次執行遺留的表單截圖。"""
    stale = glob.glob(os.path.join(_TMP, "nomad_form_*.png"))
    for f in stale:
        try:
            os.remove(f)
        except OSError:
            pass
    if stale:
        print(f"  已清除 {len(stale)} 張舊截圖", flush=True)


# ════════════════════════════════════════════════════════════════════════════════
# 截圖工具
# ════════════════════════════════════════════════════════════════════════════════

def capture_full_form(count):
    """
    截圖 Nomad 表單所有頁面。
    Step 1: 等待表單載入完成（hash 靜止）
    Step 2: 捲回頂部（hash 驗證）
    Step 3: 逐頁截圖直到底部（hash 不變 + 多變體手勢確認）
    回傳截圖路徑清單（最多 8 頁）。
    """
    import hashlib
    from PIL import Image
    import io

    def content_hash(path, crop_top=50):
        """Hash 內容區域（跳過狀態列避免時鐘誤判）。"""
        img = Image.open(path)
        cropped = img.crop((0, crop_top, img.width, img.height))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return hashlib.md5(buf.getvalue()).hexdigest()

    # Step 1: 等待載入完成
    # 密碼對話框有時在 open_nomad_form 檢查完之後才彈出（session 剛好在此時過期），
    # 若不在這個迴圈裡持續偵測，會把對話框畫面當成「已載入」的表單內容截圖
    # （2026-07-02 案例：穆彥池外出單 4 張截圖全部停在 Notes ID Password 畫面）。
    print("    等待表單載入...", flush=True)
    load_start = time.time()
    prev_load_hash = None
    while time.time() - load_start < 30:
        if handle_notes_password_dialog():
            load_start = time.time()
            prev_load_hash = None
            continue
        tmp_path = os.path.join(_TMP, "nomad_load_check.png")
        screenshot_to_file(tmp_path)
        load_hash = content_hash(tmp_path)
        if load_hash == prev_load_hash:
            print(f"    表單已載入（{time.time() - load_start:.1f}s）", flush=True)
            break
        prev_load_hash = load_hash
        time.sleep(3)
    else:
        print("    ⚠️ 載入等待逾時，繼續執行", flush=True)

    # Step 2: 捲回頂部
    prev_top_hash = None
    for _ in range(12):
        adb("shell", "input", "swipe", "1200", "330", "1200", "630", "300")
        time.sleep(0.5)
        _top_check = os.path.join(_TMP, "nomad_top_check.png")
        screenshot_to_file(_top_check)
        top_hash = content_hash(_top_check)
        if top_hash == prev_top_hash:
            break
        prev_top_hash = top_hash
    time.sleep(0.5)

    # Step 3: 逐頁截圖
    SCROLL_VARIANTS = [
        ("1200", "620", "1200", "350", "400"),
        ("1200", "650", "1200", "310", "500"),
        ("1500", "640", "1500", "300", "700"),
    ]
    MIN_PAGES_BEFORE_BOTTOM = 2

    paths = []
    prev_hash = None

    for i in range(8):
        path = os.path.join(_TMP, f"nomad_form_{count}_{chr(ord('a') + i)}.png")
        screenshot_to_file(path)
        current_hash = content_hash(path)

        if current_hash == prev_hash:
            retried = False
            for v_idx, swipe_args in enumerate(SCROLL_VARIANTS, 1):
                adb("shell", "input", "swipe", *swipe_args)
                time.sleep(1.0)
                screenshot_to_file(path)
                retry_hash = content_hash(path)
                if retry_hash != prev_hash:
                    print(f"    [retry] 變體 {v_idx} 成功", flush=True)
                    current_hash = retry_hash
                    retried = True
                    break

            if not retried:
                try:
                    os.remove(path)
                except OSError:
                    pass
                print(f"    到達底部，共 {len(paths)} 張截圖", flush=True)
                break

        paths.append(path)
        print(f"    截圖 [{chr(ord('a') + i)}]：{path}", flush=True)
        prev_hash = current_hash

        adb("shell", "input", "swipe", *SCROLL_VARIANTS[0])
        time.sleep(0.8)
    else:
        print(f"    截圖達到上限（8 頁），共 {len(paths)} 張", flush=True)

    if not paths:
        path = os.path.join(_TMP, f"nomad_form_{count}_a.png")
        screenshot_to_file(path)
        paths = [path]

    return paths


# ════════════════════════════════════════════════════════════════════════════════
# Nomad 按鈕動態偵測
# ════════════════════════════════════════════════════════════════════════════════

def find_nomad_buttons(retry=3):
    """
    從 uiautomator dump 動態取 Nomad 按鈕列座標。
    按 x 排序：第 1 個=離開，第 2 個=核准，第 3 個=駁回。
    只有 1 個按鈕 → 已核准（只剩離開）。
    取不到時 fallback 到預設值。
    """
    BUTTON_Y_MIN, BUTTON_Y_MAX = 200, 310

    for attempt in range(retry):
        xml = dump_ui()
        pattern = r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
        candidates = []
        for m in re.finditer(pattern, xml):
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            cy = (y1 + y2) // 2
            if BUTTON_Y_MIN <= cy <= BUTTON_Y_MAX and (x2 - x1) < 600:
                cx = (x1 + x2) // 2
                candidates.append((cx, cy))

        candidates.sort(key=lambda p: p[0])

        if len(candidates) >= 1:
            result = {
                "leave":   candidates[0],
                "approve": candidates[1] if len(candidates) >= 2 else None,
                "reject":  candidates[2] if len(candidates) >= 3 else None,
            }
            print(f"    [buttons] leave={result['leave']} approve={result['approve']}", flush=True)
            return result

        print(f"    [buttons] 第 {attempt+1} 次取不到，等 2 秒重試...", flush=True)
        time.sleep(2)

    print("    [buttons] fallback 到預設座標", flush=True)
    return {
        "leave":   COORD["nomad_leave_fb"],
        "approve": COORD["nomad_approve_fb"],
        "reject":  None,
    }


def _nomad_button_count():
    """讀取 Nomad 按鈕列目前的按鈕數（3=待核准，1=已核准，0=表單已關閉）。"""
    BUTTON_Y_MIN, BUTTON_Y_MAX = 200, 310
    xml = dump_ui()
    pattern = r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
    count = 0
    for m in re.finditer(pattern, xml):
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cy = (y1 + y2) // 2
        if BUTTON_Y_MIN <= cy <= BUTTON_Y_MAX and (x2 - x1) < 600:
            count += 1
    return count


# ════════════════════════════════════════════════════════════════════════════════
# Verse 導航
# ════════════════════════════════════════════════════════════════════════════════

def launch_verse():
    print("  啟動 HCL Verse...", flush=True)
    adb("shell", "am", "start", "-n", "com.lotus.sync.traveler/.LotusTraveler")
    time.sleep(3)


def sync_now():
    """點 ⋮ → Sync Now 觸發伺服器同步。"""
    xml = dump_ui()
    mo = re.search(r'content-desc="More options"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if mo:
        cx = (int(mo.group(1)) + int(mo.group(3))) // 2
        cy = (int(mo.group(2)) + int(mo.group(4))) // 2
        adb("shell", "input", "tap", str(cx), str(cy))
    else:
        adb("shell", "input", "tap", "2350", "115")
    time.sleep(1)

    xml = dump_ui()
    sn = re.search(r'text="Sync Now"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if sn:
        cx = (int(sn.group(1)) + int(sn.group(3))) // 2
        cy = (int(sn.group(2)) + int(sn.group(4))) // 2
        adb("shell", "input", "tap", str(cx), str(cy))
        print("  Sync Now 已觸發", flush=True)
    else:
        print("  警告：找不到 Sync Now，按 Back 關閉選單", flush=True)
        adb("shell", "input", "keyevent", "4")
    time.sleep(2)


def _in_unsigned_list(xml=None):
    """判斷目前是否在 Unsigned 信件列表頁（排除 Folders 列表頁）。"""
    if xml is None:
        xml = dump_ui()
    return ('text="Unsigned"' in xml
            and 'text="Folders"' not in xml
            and 'text="Subscribe"' not in xml
            and ('id/toolbar' in xml or 'content-desc="More options"' in xml))


def _tap_text(xml, text, fallback=None, delay=2):
    """從 dump XML 找指定 text 節點並 tap 中心點；找不到用 fallback 座標。"""
    m = re.search(
        rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if m:
        cx = (int(m.group(1)) + int(m.group(3))) // 2
        cy = (int(m.group(2)) + int(m.group(4))) // 2
        tap(cx, cy, delay=delay)
        return True
    if fallback:
        tap(*fallback, delay=delay)
        return True
    return False


def ensure_clean_state():
    """按 Back 2-3 次，確保不在任何開著的信件或子選單內。"""
    for _ in range(3):
        if _in_unsigned_list():
            return
        press_back(delay=1.5)
    time.sleep(1)


def navigate_to_unsigned(_depth=0):
    """從任何畫面導航到 Unsigned 資料夾（狀態感知，最多 7 層遞迴）。"""
    if _depth > 7:
        print("  警告：導航到 Unsigned 失敗（重試超過上限）", flush=True)
        return False

    xml = dump_ui()

    if _in_unsigned_list(xml):
        print("  已在 Unsigned 資料夾", flush=True)
        return True

    if 'text="Unsigned"' in xml and 'text="Subscribe"' in xml:
        print("  Unsigned 尚未訂閱，點 Subscribe...", flush=True)
        _tap_text(xml, "Subscribe", delay=10)
        return navigate_to_unsigned(_depth + 1)

    if 'package="com.lotus.nomad"' in xml:
        print("  仍在 Nomad app，切回 Verse...", flush=True)
        launch_verse()
        return navigate_to_unsigned(_depth + 1)

    if 'text="Message"' in xml:
        print("  在信件檢視頁，按 Back 回列表...", flush=True)
        press_back(delay=2)
        return navigate_to_unsigned(_depth + 1)

    if 'text="Folders"' in xml:
        if 'text="Unsigned"' in xml:
            print("  在 Folders 列表，點 Unsigned...", flush=True)
            _tap_text(xml, "Unsigned", delay=2)
            return navigate_to_unsigned(_depth + 1)
        print("  在 Folders 列表，往下捲找 Unsigned...", flush=True)
        for _ in range(3):
            adb("shell", "input", "swipe", "1200", "800", "1200", "300", "500")
            time.sleep(1)
            xml2 = dump_ui()
            if 'text="Unsigned"' in xml2:
                _tap_text(xml2, "Unsigned", delay=2)
                return navigate_to_unsigned(_depth + 1)
        print("  警告：捲完仍找不到 Unsigned，使用固定座標...", flush=True)
        tap(1336, 578, delay=2)
        return navigate_to_unsigned(_depth + 1)

    if 'text="Mail"' in xml:
        print("  從主畫面進入 Mail...", flush=True)
        tap(*COORD["main_mail"], delay=2)
        xml = dump_ui()

    if 'text="Inbox"' in xml or 'id/toolbar' in xml:
        print("  開選單 → Folders → Unsigned...", flush=True)
        tap(*COORD["hamburger"], delay=1)
        time.sleep(0.5)
        _tap_text(dump_ui(), "Folders", fallback=COORD["menu_folders"], delay=1.5)
        time.sleep(0.5)
        return navigate_to_unsigned(_depth + 1)

    if _depth < 2:
        print("  無法識別目前畫面，等 2 秒重新判斷...", flush=True)
        time.sleep(2)
        return navigate_to_unsigned(_depth + 1)

    print("  警告：無法識別目前畫面，嘗試重新啟動", flush=True)
    launch_verse()
    time.sleep(1)
    return navigate_to_unsigned(_depth + 1)


# ════════════════════════════════════════════════════════════════════════════════
# 信件列表解析
# ════════════════════════════════════════════════════════════════════════════════

def get_email_list():
    """讀取目前畫面上可見的信件列表。回傳 [(cx, cy, subject), ...] 按 y 排列。"""
    xml = dump_ui()
    subj_pattern = (
        r'text="([^"]*)"[^>]*'
        r'resource-id="com\.lotus\.sync\.traveler:id/email_subject"[^>]*'
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
    )
    items = []
    for m in re.finditer(subj_pattern, xml):
        text = m.group(1)
        x1, y1, x2, y2 = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        cy = (y1 + y2) // 2
        items.append((1268, cy, text))
    items.sort(key=lambda x: x[1])
    return items


def scroll_to_top_list():
    adb("shell", "input", "swipe", "1200", "300", "1200", "900", "400")
    time.sleep(1)


def scroll_down_list():
    adb("shell", "input", "swipe", "1200", "800", "1200", "300", "400")
    time.sleep(1)


def find_next_email(processed, subject_filter=None):
    """
    在 Unsigned 列表中找第一封尚未處理的信件（支援捲動）。
    processed: {subject: 已處理封數}
    subject_filter: 若指定，只返回此集合內的主旨（None = 不限制）
    回傳 (cx, cy, subject) 或 None。
    """
    scroll_to_top_list()
    time.sleep(1.5)
    occurrence = {}
    prev_texts = []

    for _ in range(10):
        items = get_email_list()
        texts = [t for _, _, t in items]

        if prev_texts and texts == prev_texts:
            break

        new_items = items
        if prev_texts:
            max_k = min(len(prev_texts), len(texts))
            for k in range(max_k, 0, -1):
                if prev_texts[-k:] == texts[:k]:
                    new_items = items[k:]
                    break

        for cx, cy, text in new_items:
            if not any(k in text for k in APPROVAL_KEYWORDS):
                print(f"    [skip] 主旨不符關鍵字，略過：{text[:40]}", flush=True)
                processed[text] = processed.get(text, 0) + 1
                continue
            if subject_filter is not None and text not in subject_filter:
                continue
            occurrence[text] = occurrence.get(text, 0) + 1
            if occurrence[text] > processed.get(text, 0):
                return (cx, cy, text)

        prev_texts = texts
        scroll_down_list()

    return None


# ════════════════════════════════════════════════════════════════════════════════
# Nomad 表單操作
# ════════════════════════════════════════════════════════════════════════════════

def get_form_type(subject):
    """從主旨判斷表單類型，回傳 '加班申請' / '外出單' / '未刷卡' / None。"""
    if "未刷卡" in subject:
        return "未刷卡"
    if "加班申請" in subject:
        return "加班申請"
    if "外出單" in subject:
        return "外出單"
    return None


def handle_notes_password_dialog():
    """偵測並處理 Notes ID Password 對話框，自動輸入密碼。"""
    xml = dump_ui()
    if 'Notes ID Password' not in xml and 'Notes ID password' not in xml:
        return False

    print("    偵測到 Notes ID Password 對話框，自動輸入密碼...", flush=True)
    pw_match = re.search(
        r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if pw_match:
        cx = (int(pw_match.group(1)) + int(pw_match.group(3))) // 2
        cy = (int(pw_match.group(2)) + int(pw_match.group(4))) // 2
        tap(cx, cy, delay=0.5)
    else:
        tap(1200, 540, delay=0.5)

    adb("shell", "input", "keyevent", "277")  # KEYCODE_CTRL_A
    adb("shell", "input", "keyevent", "67")   # KEYCODE_DEL
    time.sleep(0.3)

    DIGIT_KEYCODES = {'0':7,'1':8,'2':9,'3':10,'4':11,'5':12,'6':13,'7':14,'8':15,'9':16}
    for ch in NOTES_PASSWORD:
        kc = DIGIT_KEYCODES.get(ch)
        if kc:
            adb("shell", "input", "keyevent", str(kc))
        else:
            adb("shell", "input", "text", ch)
        time.sleep(0.1)

    adb("shell", "input", "keyevent", "66")
    print("    密碼已送出，等待 Nomad 載入...", flush=True)
    time.sleep(5)

    xml_after = dump_ui()
    if 'Wrong Password' in xml_after or 'wrong password' in xml_after.lower():
        print("    ✗ Notes ID 密碼錯誤！", flush=True)
        _tap_text(xml_after, "OK", delay=1)
        raise PasswordError("Notes ID 密碼錯誤，請確認 HCL_NOTES_PASSWORD 環境變數")
    return True


def _current_foreground_pkg():
    """讀取目前前景 app package。"""
    out = adb("shell", "dumpsys", "activity", "activities")
    m = re.search(r"mResumedActivity.*?(\S+)/", out)
    if m:
        return m.group(1).split()[-1]
    m = re.search(r"topResumedActivity.*?(\S+)/", out)
    if m:
        return m.group(1).split()[-1]
    return ""


def _try_open_link_text(timeout=6):
    """Fallback：找 'Link' 超連結文字節點並點擊（純通知信常見）。"""
    xml = dump_ui()
    for m in re.finditer(
        r'(?:text|content-desc)="Link"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        print(f"    [fallback] 改點 Link 文字 ({cx}, {cy})", flush=True)
        tap(cx, cy, delay=timeout)
        return True
    return False


def _open_nomad_from_chrome_url():
    """若 Chrome 被開啟，從 address bar 取 URL → 轉 notes:// → am start 開 Nomad。"""
    xml = dump_ui()
    portal_domain = os.environ.get("HCL_PORTAL_HOST", "portal.ecic.com.tw")
    url_match = None
    for m in re.finditer(r'text="([^"]*' + re.escape(portal_domain) + r'[^"]*)"', xml):
        url_match = m.group(1)
        break
    if not url_match:
        print("    ⚠️ Chrome 未找到 portal URL", flush=True)
        return False

    https_url = url_match if url_match.startswith("http") else "https://" + url_match
    notes_url = re.sub(r'^https?://', 'notes://', https_url)
    print(f"    [notes intent] {notes_url[:80]}...", flush=True)

    adb("shell", "input", "keyevent", "4")
    time.sleep(1)

    adb("shell", "am", "start",
        "-a", "android.intent.action.VIEW",
        "-d", notes_url,
        "-n", "com.lotus.nomad/com.lotus.noteslib.core.MainActivity")
    time.sleep(6)
    pkg = _current_foreground_pkg()
    return "nomad" in pkg.lower()


def _find_link_bounds():
    """尋找信件內文 'Link' 超連結節點的 bounds。"""
    xml = dump_ui()
    m = re.search(
        r'(?:text|content-desc)="Link"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if m:
        return tuple(int(m.group(i)) for i in range(1, 5))
    return None


def _find_attach_icon_bounds(retry=15, delay=1.0):
    """
    尋找信件內文附件圖示節點：content-desc="Open link in HCL Notes (Document Link)"，
    這是 Verse 內文 WebView 對附件圖示的語意標籤，比用 'Link' 文字位置推算偏移量準確。

    剛點開信件時 WebView 常常還沒 layout 完成，uiautomator dump 會量到高度為 0 的
    暫時性 bounds（例如 [529,576][611,576]，y1==y2），此時算出的座標不可信。
    這裡重試等待 bounds 收斂成非零高度才回傳（2026-07-02 案例：同一封信連續兩次
    dump 都撈到零高度 bounds，改點算出的座標完全點不中任何東西）。

    retry=15（約 15 秒）：實測同一批信件中 WebView 渲染時間落差很大，
    有的 3~4 秒就緒、有的需要 14 秒以上（伺服器回應速度或裝置負載影響），
    重試次數太少會在正常情況下也誤判為「找不到」。
    """
    pattern = re.compile(
        r'content-desc="Open link in HCL Notes \(Document Link\)"'
        r'[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
    )
    for _ in range(retry):
        xml = dump_ui()
        m = pattern.search(xml)
        if m:
            x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
            if y2 > y1 and x2 > x1:
                return (x1, y1, x2, y2)
        time.sleep(delay)
    return None


def open_nomad_form(email_cx, email_cy):
    """點開信件 → 點附件圖示開啟 Nomad，處理密碼對話框與各種 fallback。"""
    print(f"    點開信件 ({email_cx}, {email_cy})", flush=True)
    tap(email_cx, email_cy, delay=2.5)

    # 優先用附件圖示的語意標籤動態定位，比固定座標或由 Link 位置推算偏移量準確，
    # 且內建等待 WebView layout 完成的重試機制。
    icon_bounds = _find_attach_icon_bounds()
    if icon_bounds:
        x1, y1, x2, y2 = icon_bounds
        icon_x, icon_y = (x1 + x2) // 2, (y1 + y2) // 2
        print(f"    點附件圖示（動態定位 {icon_bounds}） ({icon_x}, {icon_y})", flush=True)
        tap(icon_x, icon_y, delay=5)
    else:
        print(f"    找不到附件圖示節點，改點固定座標 {COORD['attach_icon']}", flush=True)
        tap(*COORD["attach_icon"], delay=5)
    handle_notes_password_dialog()

    pkg = _current_foreground_pkg()
    if "nomad" in pkg.lower():
        return

    print(f"    ⚠️ 仍未進入 Nomad（前景：{pkg or '未知'}），嘗試點 Link 文字", flush=True)
    if _try_open_link_text():
        handle_notes_password_dialog()
        pkg = _current_foreground_pkg()
        if "nomad" in pkg.lower():
            return
        if "chrome" in pkg.lower():
            print("    Chrome 攔截了連結，改用 notes:// intent...", flush=True)
            if _open_nomad_from_chrome_url():
                handle_notes_password_dialog()
                return
        print(f"    ⚠️ Link 點擊後仍未進入 Nomad（前景：{pkg}）", flush=True)
    else:
        print("    ⚠️ 找不到 Link 文字節點，將截到 Verse email view", flush=True)

    # 所有 fallback 都失敗：明確拋例外讓上層標記 error/approve_failed，
    # 留在 Unsigned 供下次執行的遺留檢查接手，而不是靜默對著錯誤畫面（例如
    # Chrome 意外開啟的網頁）繼續截圖、產生看似成功但內容錯誤的結果。
    raise FormOpenError(f"無法開啟 Nomad 表單，前景 app 為：{pkg or '未知'}")


def do_approve(buttons):
    """
    執行核准：核准 → Comments OK → 遞送 OK。
    最終驗證按鈕列：核准/駁回按鈕應消失（剩 0~1 個）。
    """
    approve_coord = buttons.get("approve") or COORD["nomad_approve_fb"]
    print(f"    執行核准 {approve_coord}...", flush=True)
    tap(*approve_coord, delay=2)
    tap(*COORD["comments_ok"], delay=2)
    tap(*COORD["delivery_ok"], delay=3)

    xml = dump_ui()
    if 'text="OK"' in xml:
        print("    偵測到殘留對話框，補按 OK...", flush=True)
        _tap_text(xml, "OK", delay=2)

    for attempt in range(3):
        n = _nomad_button_count()
        if n <= 1:
            xml = dump_ui()
            if 'Wrong Password' in xml or 'Notes ID Password' in xml or 'Notes ID password' in xml:
                print("    ✗ 偵測到密碼相關對話框", flush=True)
                _tap_text(xml, "OK", delay=1)
                raise PasswordError("核准時仍顯示密碼錯誤對話框")
            print(f"    核准驗證通過（按鈕列剩 {n} 個按鈕）", flush=True)
            return "approved"
        print(f"    [verify] 按鈕列仍有 {n} 個按鈕，等 2 秒...", flush=True)
        time.sleep(2)

    print("    ⚠️ 核准後按鈕列仍有核准按鈕，標記 approve_failed", flush=True)
    return "approve_failed"


def do_leave(buttons):
    """點 Nomad 離開按鈕（通知信或已核准表單）。"""
    leave_coord = buttons.get("leave") or COORD["nomad_leave_fb"]
    print(f"    點離開 {leave_coord}...", flush=True)
    tap(*leave_coord, delay=2)
    return "already_approved"


def back_to_unsigned():
    """從 Nomad 表單回到 Unsigned 列表。"""
    time.sleep(1)
    if not _in_unsigned_list():
        press_back(delay=2)
    navigate_to_unsigned()
    time.sleep(2)


def _get_buttons_for(subject, is_notif=False):
    """根據主旨取按鈕座標（優先用預設值，取不到再動態偵測）。"""
    form_type = get_form_type(subject)
    if form_type:
        preset = FORM_BUTTONS[form_type]
        return {
            "leave":   preset["leave"],
            "approve": None if is_notif else preset["approve"],
        }
    return find_nomad_buttons()


# ════════════════════════════════════════════════════════════════════════════════
# Phase 2a — 截圖（不核准）
# ════════════════════════════════════════════════════════════════════════════════

def _screenshot_one_email(cx, cy, subject, count):
    """
    開啟表單 → 截圖所有頁面 → 離開回 Unsigned（不核准）。
    回傳截圖路徑清單。
    """
    open_nomad_form(cx, cy)

    # 收鍵盤（comment 欄位 auto-focus 會彈鍵盤蓋住表單）
    ime_status = adb("shell", "dumpsys", "input_method")
    if "mInputShown=true" in ime_status:
        adb("shell", "input", "keyevent", "4")
        time.sleep(1.0)

    screenshots = capture_full_form(count)

    # 離開表單（不核准）
    buttons = _get_buttons_for(subject, is_notif=True)  # is_notif=True → approve=None
    do_leave(buttons)

    return screenshots


def phase2a_screenshot_all(subject_filter=None):
    """
    Phase 2a：逐封開啟 Unsigned 信件，截圖後回到 Unsigned（不核准）。

    subject_filter: 若指定（set of str），只處理這些主旨（retry 用）；
                    None 表示處理所有 Unsigned 中的信件。

    輸出：hcl_screenshots.json（追加模式：既有截圖不覆蓋，新增或更新）
    回傳 [{subject, screenshots: [paths]}]
    """
    print("\n═══ Phase 2a：截圖所有表單 ═══", flush=True)
    if subject_filter:
        print(f"  （retry 模式：只處理 {len(subject_filter)} 封）", flush=True)

    if not NOTES_PASSWORD:
        print("  ✗ HCL_NOTES_PASSWORD 未設定，無法操作 Nomad", flush=True)
        return []

    # 讀取既有截圖結果（retry 時保留已完成的）
    screenshots_path = os.path.join(_TMP, "hcl_screenshots.json")
    existing = {}
    if os.path.exists(screenshots_path) and subject_filter:
        with open(screenshots_path) as f:
            for item in json.load(f):
                existing[item["subject"]] = item

    launch_verse()
    ensure_clean_state()
    navigate_to_unsigned()
    time.sleep(2)
    sync_now()
    ensure_clean_state()
    navigate_to_unsigned()
    time.sleep(2)

    processed = {}
    results_map = dict(existing)  # subject → {subject, screenshots}
    # 用既有截圖檔名中出現過的最大編號起算，而不是 len(existing)：
    # 同一輪多次個別 retry 時，existing 筆數不變（更新既有 subject 的內容），
    # 若用筆數當編號，兩次不同 subject 的 retry 會算出同一個編號，導致
    # 後寫入的截圖檔案覆蓋掉前一個 subject 的檔案，兩個 subject 最後指向同一批
    # 錯誤截圖（2026-07-02 案例：劉子瑜 2026/7/2 外出單被穆彥池外出單的截圖覆蓋）。
    count = 0
    for item in existing.values():
        for p in item.get("screenshots", []):
            m = re.search(r"nomad_form_(\d+)_", os.path.basename(p))
            if m:
                count = max(count, int(m.group(1)))

    while True:
        next_email = find_next_email(processed, subject_filter=subject_filter)
        if not next_email:
            break

        cx, cy, subject = next_email
        count += 1
        print(f"\n  [{count}] {subject[:50]}", flush=True)

        try:
            screenshots = _screenshot_one_email(cx, cy, subject, count)
            results_map[subject] = {"subject": subject, "screenshots": screenshots}
            print(f"    → {len(screenshots)} 張截圖", flush=True)
        except PasswordError as e:
            print(f"    ✗ 密碼錯誤，停止：{e}", flush=True)
            results_map[subject] = {"subject": subject, "screenshots": [], "error": "password_error"}
            break
        except Exception as e:
            print(f"    ✗ 失敗：{e}", flush=True)
            results_map[subject] = {"subject": subject, "screenshots": [], "error": str(e)}
            try:
                ensure_clean_state()
            except Exception:
                pass

        processed[subject] = processed.get(subject, 0) + 1

        try:
            back_to_unsigned()
        except Exception as e:
            print(f"    ⚠️ 返回 Unsigned 失敗：{e}", flush=True)
            launch_verse()
            ensure_clean_state()
            navigate_to_unsigned()
        time.sleep(1)

    results = list(results_map.values())

    with open(screenshots_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n  Phase 2a 完成：共 {len(results)} 封截圖", flush=True)
    return results


# ════════════════════════════════════════════════════════════════════════════════
# Phase 2b — 核准（讀取 hcl_verified.json）
# ════════════════════════════════════════════════════════════════════════════════

def _approve_one_email(cx, cy, subject):
    """
    開啟表單 → 核准（或對通知信點離開）。
    回傳 status string。
    """
    is_notif = "通知" in subject
    open_nomad_form(cx, cy)

    buttons = _get_buttons_for(subject, is_notif=is_notif)
    has_approve = buttons.get("approve") is not None

    if is_notif or not has_approve:
        do_leave(buttons)
        return "notification" if is_notif else "already_approved"
    else:
        return do_approve(buttons)


def phase2b_approve_verified():
    """
    Phase 2b：讀取 hcl_verified.json，核准已驗證的信件，跳過未驗證的。
    輸出：hcl_approve_results.json
    回傳 [{subject, status}]
    """
    print("\n═══ Phase 2b：核准已驗證的表單 ═══", flush=True)

    if not NOTES_PASSWORD:
        print("  ✗ HCL_NOTES_PASSWORD 未設定，無法執行核准", flush=True)
        return []

    verified_path = os.path.join(_TMP, "hcl_verified.json")
    if not os.path.exists(verified_path):
        print("  找不到 hcl_verified.json，請先由 Claude skill 層寫入", flush=True)
        return []

    with open(verified_path, encoding='utf-8') as f:
        verified_list = json.load(f)

    approved_subjects = {v["subject"] for v in verified_list if v.get("ok")}
    skipped_subjects  = {v["subject"] for v in verified_list if not v.get("ok")}

    print(f"  已驗證 {len(approved_subjects)} 封，跳過 {len(skipped_subjects)} 封", flush=True)
    if skipped_subjects:
        print("  ⚠️ 以下信件截圖欄位不完整，已跳過（保留在 Unsigned）：", flush=True)
        for s in skipped_subjects:
            print(f"    - {s}", flush=True)

    results = []

    if approved_subjects:
        launch_verse()
        ensure_clean_state()
        navigate_to_unsigned()
        time.sleep(2)

        processed = {}

        while True:
            next_email = find_next_email(processed, subject_filter=approved_subjects)
            if not next_email:
                break

            cx, cy, subject = next_email
            print(f"\n  {subject[:50]}", flush=True)

            try:
                status = _approve_one_email(cx, cy, subject)
            except PasswordError as e:
                print(f"    ✗ 密碼錯誤，停止：{e}", flush=True)
                results.append({"subject": subject, "status": "password_error"})
                break
            except Exception as e:
                print(f"    ✗ 失敗：{e}", flush=True)
                status = "error"
                try:
                    ensure_clean_state()
                except Exception:
                    pass

            processed[subject] = processed.get(subject, 0) + 1
            results.append({"subject": subject, "status": status})
            print(f"    → {status}", flush=True)

            try:
                back_to_unsigned()
            except Exception as e:
                print(f"    ⚠️ 返回 Unsigned 失敗：{e}", flush=True)
                launch_verse()
                ensure_clean_state()
                navigate_to_unsigned()
            time.sleep(1)

    # 跳過的信件也加入結果（status=screenshot_failed）
    for subject in skipped_subjects:
        results.append({"subject": subject, "status": "screenshot_failed"})

    with open(os.path.join(_TMP, "hcl_approve_results.json"), "w") as f:
        json.dump({"total": len(results), "results": results}, f, ensure_ascii=False, indent=2)

    print(f"\n  Phase 2b 完成：{len(results)} 封處理完畢", flush=True)
    return results


# ════════════════════════════════════════════════════════════════════════════════
# 主程式
# ════════════════════════════════════════════════════════════════════════════════

def main():
    if "--screenshot-only" in sys.argv:
        # retry 模式：若有 hcl_retry_subjects.json，只處理指定主旨
        retry_path = os.path.join(_TMP, "hcl_retry_subjects.json")
        subject_filter = None
        if os.path.exists(retry_path):
            with open(retry_path, encoding="utf-8") as f:
                subjects = json.load(f)
            if subjects:
                subject_filter = set(subjects)
                print(f"  retry 模式：{len(subject_filter)} 封需重新截圖", flush=True)

        # 首次執行前清除舊截圖（retry 時保留，由 phase2a 內部合併）
        if subject_filter is None:
            clear_stale_screenshots()

        phase2a_screenshot_all(subject_filter=subject_filter)

    elif "--approve" in sys.argv:
        phase2b_approve_verified()

    else:
        print("用法：hcl_approve_android.py --screenshot-only | --approve", flush=True)


if __name__ == "__main__":
    main()
