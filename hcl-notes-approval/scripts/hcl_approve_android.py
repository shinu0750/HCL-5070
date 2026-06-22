#!/usr/bin/env python3
"""
Phase 2 (Android) — 透過 DroidMind MCP / ADB 控制 Android 模擬器
在 HCL Verse Unsigned 資料夾中，逐一開啟信件 → Nomad 表單 → 截圖 OCR 讀取內容 → 核准或離開

座標系統：橫向 rotation=1，邏輯座標 2400×1080

Nomad 按鈕動態取座標說明：
  - 按鈕列在 y=[205,299]（center y=252），uiautomator 可讀取（非 WebView）
  - 按鈕文字寬度不同導致 x 座標隨表單類型變動：
      加班/外出申請單：離開[148-339] 核准[352-543] 駁回[556-747]
      未刷卡申請單：   離開(exit)[148-430] 核准[443-634] 駁回[646-837]
  - 按 x 排序：第 1 個=離開，第 2 個=核准，第 3 個=駁回
  - 只有 1 個按鈕 → 已核准（只剩離開）；有 3 個 → 待核准
"""

import base64, glob, json, os, re, subprocess, sys, tempfile, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 載入 .env
_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

SERIAL       = "emulator-5554"
_adb_win = r"C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
_adb_mac = "/Users/shuhsing/Library/Android/sdk/platform-tools/adb"
ADB_PATH     = _adb_win if os.path.exists(_adb_win) else _adb_mac
_TMP         = tempfile.gettempdir()
NOTES_PASSWORD = os.environ.get("HCL_NOTES_PASSWORD", "")

# ── 固定座標（橫向 2400×1080）──────────────────────────────────────────────────
COORD = {
    # Verse 主畫面
    "main_mail":        (1268, 275),
    # 漢堡選單 ☰
    "hamburger":        (198,  115),
    # 側邊選單 Folders
    "menu_folders":     (330,  846),
    # Folders 列表 — Unsigned 資料夾
    "folder_unsigned":  (1326, 757),
    # 信件內 📄 附件圖示（WebView，固定位置）
    "attach_icon":      (415,  700),
    # Comments 對話框 OK（uiautomator 可讀，固定）
    "comments_ok":      (1604, 753),
    # 遞送完成確認 OK（uiautomator 可讀，固定）
    "delivery_ok":      (1871, 660),
    # Nomad 按鈕列 fallback（uiautomator 取不到時使用）
    "nomad_leave_fb":   (243,  252),
    "nomad_approve_fb": (447,  252),
}

# ── 各表單類型預設按鈕座標（y=252 固定，x 因按鈕文字寬度不同）──────────────────
FORM_BUTTONS = {
    "加班申請": {"leave": (243, 252), "approve": (447, 252)},
    "外出單":   {"leave": (243, 252), "approve": (447, 252)},
    "未刷卡":   {"leave": (289, 252), "approve": (538, 252)},
}

# ── 各表單類型必要欄位（v1.3.1 截圖完整性驗證用）─────────────────────────────────
# 每個欄位是 (label, [acceptable_patterns]) — OCR 文字去空白後，patterns 任一命中即算截到。
# Vision OCR 有時會把直書欄位名（如「姓 名」）拆到不同行、中間插入其它欄位文字，
# 所以「姓名」連續字串可能找不到，需 fallback 到「名：」這類資料行特徵。
FORM_REQUIRED_FIELDS = {
    "加班申請": [
        ("工號",           ["工號", r"號[：:]\s*\d"]),
        ("姓名",           ["姓名", "名："]),
        ("部門",           ["部門"]),
        ("事由",           ["事由"]),
        ("加班歸屬日期",   ["加班歸屬日期", "歸屬日期"]),
        ("類別",           ["類別"]),
        ("加班起訖日期",   ["加班起訖日期", "起訖日期"]),
        ("加班起訖時間",   ["加班起訖時間", "起訖時間"]),
        ("申請加班時數",   ["申請加班時數", "加班時數"]),
        ("申請加班費或轉休", ["申請加班費或轉休", "加班費或轉休"]),
    ],
    "外出單": [
        ("工號",         ["工號", r"號[：:]\s*\d"]),
        ("姓名",         ["姓名", "名："]),
        ("部門",         ["部門"]),
        ("外出事由",     ["外出事由"]),
        ("外出地點",     ["外出地點"]),
        ("外出起訖日期", ["外出起訖日期", "起訖日期", "開始日期", "結束日期"]),
        ("外出起訖時間", ["外出起訖時間", "起訖時間", "開始時間", "結束時間"]),
    ],
    "未刷卡": [
        ("工號",       ["工號", r"號[：:]\s*\d"]),
        ("姓名",       ["姓名", "名："]),
        ("部門",       ["部門"]),
        ("未刷卡原因", ["未刷卡原因"]),
        ("未刷卡說明", ["未刷卡說明"]),
        ("未刷卡日期", ["未刷卡日期"]),
        ("未刷卡時間", ["未刷卡時間"]),
    ],
}

def get_form_type(subject):
    """從主旨判斷表單類型，回傳 '加班申請' / '外出單' / '未刷卡' / None"""
    if "未刷卡" in subject:
        return "未刷卡"
    if "加班申請" in subject:
        return "加班申請"
    if "外出單" in subject:
        return "外出單"
    return None

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


def screenshot_b64():
    """取得截圖，回傳 base64 字串（供 AI 視覺判斷）"""
    raw = subprocess.run(
        [ADB_PATH, "-s", SERIAL, "shell", "screencap", "-p"],
        capture_output=True, timeout=15
    ).stdout
    return base64.b64encode(raw).decode()


def screenshot_to_file(path=None):
    if path is None:
        path = os.path.join(_TMP, "nomad_form.png")
    """截圖存到本機檔案"""
    adb("shell", "screencap", "-p", "/sdcard/screen.png")
    subprocess.run([ADB_PATH, "-s", SERIAL, "pull", "/sdcard/screen.png", path],
                   capture_output=True)
    return path


# ── macOS Vision OCR（v1.3.1 截圖完整性驗證）─────────────────────────────────
_VISION_LOADED = False

def _ensure_vision():
    """Lazy-load macOS Vision framework via pyobjc。失敗回傳 False。"""
    global _VISION_LOADED
    if _VISION_LOADED:
        return True
    try:
        from objc import loadBundle
        loadBundle('Vision', globals(),
                   bundle_path='/System/Library/Frameworks/Vision.framework')
        _VISION_LOADED = True
        return True
    except Exception as e:
        print(f"    [ocr] Vision 載入失敗：{e}（將跳過內容驗證）", flush=True)
        return False


def ocr_image(path):
    """用 macOS Vision 對截圖做中文 OCR，回傳全文字串（行以 \\n 分隔）。失敗回傳 ''。"""
    if not _ensure_vision():
        return ""
    try:
        from Foundation import NSURL
        url = NSURL.fileURLWithPath_(path)
        req = VNRecognizeTextRequest.alloc().init()  # noqa: F821
        req.setRecognitionLevel_(0)  # Accurate
        req.setRecognitionLanguages_(["zh-Hant", "zh-Hans", "en-US"])
        req.setUsesLanguageCorrection_(False)
        handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)  # noqa: F821
        handler.performRequests_error_([req], None)
        lines = []
        for obs in (req.results() or []):
            cand = obs.topCandidates_(1)
            if cand:
                lines.append(str(cand[0].string()))
        return "\n".join(lines)
    except Exception as e:
        print(f"    [ocr] OCR 失敗：{e}", flush=True)
        return ""


def _coverage_missing(seen_text, required_fields):
    """
    回傳尚未在 seen_text 出現的欄位 label list。required_fields=None 時回傳 []。
    required_fields 格式：[(label, [pattern1, pattern2, ...]), ...]
      — pattern 可以是純字串或 regex；任一命中即視為該欄位已截到。
    比對前會把空白移除（OCR 常把「工 號」插空格），所以 patterns 也要相應地無空白。
    """
    if not required_fields:
        return []
    normalized = re.sub(r"\s+", "", seen_text)
    missing = []
    for label, patterns in required_fields:
        hit = False
        for p in patterns:
            try:
                if re.search(p, normalized):
                    hit = True
                    break
            except re.error:
                if p in normalized:
                    hit = True
                    break
        if not hit:
            missing.append(label)
    return missing


def capture_full_form(count, required_fields=None):
    """
    Step 1: 等待 Nomad 表單載入完成（Domino 一次渲染完成，無需預捲預載）
    Step 2: 捲回頂部
    Step 3: 逐頁往下截圖，每張 OCR 累積必要欄位，
            欄位收齊或連續手勢無效 → 已到底 → 停止

    參數：
      count            — 表單流水號（截圖檔名用）
      required_fields  — 必要欄位 list（取自 FORM_REQUIRED_FIELDS[form_type]），
                          可為 None；非 None 時每張截圖會 OCR 驗證涵蓋率

    回傳截圖路徑清單 [nomad_form_{count}_a.png, _b.png, ...]，最多 8 頁。
    """
    import hashlib
    from PIL import Image
    import io

    def content_hash(path, crop_top=50):
        """Hash only the content area (skip status bar) to avoid clock-tick false positives."""
        img = Image.open(path)
        cropped = img.crop((0, crop_top, img.width, img.height))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return hashlib.md5(buf.getvalue()).hexdigest()

    # ── Step 1: 等待載入完成 ──────────────────────────────────────────────────
    # Nomad "Please wait..." 動畫持續變化，載入完成後畫面靜止。
    # 每 3 秒截一張；連續兩張 hash 相同 → 靜止 = 表單已就緒。
    # 使用 3 秒間隔確保 spinner 不會恰好轉回同位置造成誤判。
    print("    等待表單載入...", flush=True)
    load_timeout = 30
    load_start = time.time()
    prev_load_hash = None
    while time.time() - load_start < load_timeout:
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

    # ── Step 2: 捲回頂部（hash 驗證，確保真的到頂）──────────────────────────────
    # 手勢必須落在 WebView 內容區內：工具列在 y≤310（核准/離開/駁回按鈕 bounds 到 y=299），
    # 底部「外出申請單」chip 在 y≥860；故起訖點取 y=450↔820，避免空滑或誤觸按鈕。
    # 由上往下滑 → 內容下移 → 顯露頂部；連續兩張 hash 相同 = 已到頂 → 停止。
    # 修正前：起點 y=200 壓在工具列上，滑動沒傳進 WebView，盲捲 5 次常停在下半段。
    prev_top_hash = None
    for _ in range(12):  # 上限 12 次，足夠捲完最長表單
        adb("shell", "input", "swipe", "1200", "330", "1200", "630", "300")
        time.sleep(0.5)
        _top_check = os.path.join(_TMP, "nomad_top_check.png")
        screenshot_to_file(_top_check)
        top_hash = content_hash(_top_check)
        if top_hash == prev_top_hash:
            break  # 捲動後畫面未變 = 已到頂
        prev_top_hash = top_hash
    time.sleep(0.5)

    # ── Step 3: 逐頁截圖直到底部（v1.3：截圖後驗證捲動是否成功）─────────────
    # 設計：每次「截圖 → 比對 → 捲動 → 重截 → 驗證」一個完整動作循環：
    #   1. 截圖 current
    #   2. 與 prev_hash 比對：相同 = 上一次捲動沒效果（或真到底）
    #      → 用「更強手勢」重試捲動（起點更高、終點更低、duration 更長）
    #      → 連續 RETRY_LIMIT 次捲完仍未變 → 確認到底
    #   3. 不同 → 視為新一頁，存檔，繼續
    # 修正前：第一次截圖後立刻捲一次，第二張若 hash 相同就直接 break，
    #         導致「捲動沒生效（短表單／簽核完成唯讀模式）」被誤判為「已到底」，
    #         漏截日期/時間/事由等下半段欄位（form 4、form 6 受害）。
    SCROLL_VARIANTS = [
        ("1200", "620", "1200", "350", "400"),   # 標準：WebView 內容區中段
        ("1200", "650", "1200", "310", "500"),   # 加強：較大行程、較慢
        ("1500", "640", "1500", "300", "700"),   # 最強：靠右側、最大行程
    ]
    RETRY_LIMIT = len(SCROLL_VARIANTS)  # 3 次手勢都沒變 → 真的到底
    MIN_PAGES_BEFORE_BOTTOM = 2  # 至少要 2 張不同截圖才允許判定到底

    paths = []
    prev_hash = None
    accumulated_text = ""  # v1.3.1：累積所有截圖的 OCR 文字，用於欄位涵蓋率驗證

    for i in range(8):  # 最多 8 頁
        path = os.path.join(_TMP, f"nomad_form_{count}_{chr(ord('a') + i)}.png")
        screenshot_to_file(path)

        current_hash = content_hash(path)

        if current_hash == prev_hash:
            # 上次捲動沒效果 → 用更強手勢重試，仍無效才真正到底
            if len(paths) < MIN_PAGES_BEFORE_BOTTOM:
                print(f"    [retry] 第 {i} 頁 hash 未變且僅 {len(paths)} 張，"
                      f"加強手勢重試 (≤{MIN_PAGES_BEFORE_BOTTOM} 張下限)", flush=True)
            else:
                print(f"    [retry] 第 {i} 頁 hash 未變，加強手勢重試", flush=True)

            retried = False
            for variant_idx, swipe_args in enumerate(SCROLL_VARIANTS, 1):
                adb("shell", "input", "swipe", *swipe_args)
                time.sleep(1.0)
                screenshot_to_file(path)
                retry_hash = content_hash(path)
                if retry_hash != prev_hash:
                    print(f"    [retry] 變體 {variant_idx} 成功，捲到新一頁", flush=True)
                    current_hash = retry_hash
                    retried = True
                    break
                print(f"    [retry] 變體 {variant_idx} 仍未變", flush=True)

            if not retried:
                # 連續所有手勢都沒讓畫面變 → 確認到底
                try:
                    os.remove(path)
                except OSError:
                    pass
                missing = _coverage_missing(accumulated_text, required_fields)
                if missing:
                    print(f"    ⚠️ 到達底部但仍缺欄位 {missing}（共 {len(paths)} 張）",
                          flush=True)
                else:
                    print(f"    到達底部（{RETRY_LIMIT} 次手勢確認），共 {len(paths)} 張截圖",
                          flush=True)
                break

        # v1.3.1：對本張做 OCR，累積文字並回報尚未截到的欄位
        page_text = ocr_image(path) if required_fields else ""
        if page_text:
            accumulated_text += "\n" + page_text

        paths.append(path)
        print(f"    截圖 [{chr(ord('a') + i)}]：{path}", flush=True)

        # 涵蓋率驗證：所有必要欄位都已在 accumulated_text 中 → 不需再捲
        if required_fields:
            missing = _coverage_missing(accumulated_text, required_fields)
            if not missing:
                print(f"    ✓ 必要欄位已全部截到（{len(required_fields)} 項），停止捲動",
                      flush=True)
                break
            else:
                print(f"    [coverage] 仍缺 {len(missing)} 項：{missing[:5]}"
                      f"{'…' if len(missing) > 5 else ''}", flush=True)

        prev_hash = current_hash

        # 標準下捲手勢（重試已在 hash-fail 分支內處理）
        adb("shell", "input", "swipe", *SCROLL_VARIANTS[0])
        time.sleep(0.8)
    else:
        print(f"    截圖達到上限（8 頁），共 {len(paths)} 張", flush=True)
        if required_fields:
            missing = _coverage_missing(accumulated_text, required_fields)
            if missing:
                print(f"    ⚠️ 達上限仍缺欄位 {missing}", flush=True)

    if not paths:
        # 保險：至少回傳一張
        path = os.path.join(_TMP, f"nomad_form_{count}_a.png")
        screenshot_to_file(path)
        paths = [path]

    return paths


def wait_for_ui(pattern, timeout=8, interval=1.0):
    """輪詢 uiautomator dump，直到 XML 符合 regex pattern 或逾時。回傳 bool。（改善 #3）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = dump_ui()
        if re.search(pattern, xml):
            return True
        time.sleep(interval)
    return False


def clear_stale_screenshots():
    """清除上次執行遺留的表單截圖，避免 Phase 4 OCR 讀到舊圖（改善 #7）
    涵蓋新格式：nomad_form_{N}_{a-h}.png（改善 #9 逐頁截圖）"""
    stale = glob.glob(os.path.join(_TMP, "nomad_form_*.png"))
    for f in stale:
        try:
            os.remove(f)
        except OSError:
            pass
    if stale:
        print(f"  已清除 {len(stale)} 張舊截圖", flush=True)


# ════════════════════════════════════════════════════════════════════════════════
# Nomad 按鈕動態偵測
# ════════════════════════════════════════════════════════════════════════════════

def find_nomad_buttons(retry=3):
    """
    從 uiautomator dump 動態取 Nomad 按鈕列座標。
    按鈕列特徵：y 範圍在 [200, 310]，由左到右排列為 離開 / 核准 / 駁回。

    回傳 dict：
      {
        "leave":   (cx, cy),        # 一定存在
        "approve": (cx, cy) | None, # 待核准時存在
        "reject":  (cx, cy) | None, # 待核准時存在
      }
    取不到時回傳 fallback 座標（加班/外出申請單預設值）。
    """
    BUTTON_Y_MIN, BUTTON_Y_MAX = 200, 310

    for attempt in range(retry):
        xml = dump_ui()
        # 找所有 clickable=true 且 bounds 的 y 範圍在按鈕列內的節點
        pattern = r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
        candidates = []
        for m in re.finditer(pattern, xml):
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            cy = (y1 + y2) // 2
            # 過濾出按鈕列範圍，排除全螢幕容器（width > 1000）
            if BUTTON_Y_MIN <= cy <= BUTTON_Y_MAX and (x2 - x1) < 600:
                cx = (x1 + x2) // 2
                candidates.append((cx, cy))

        candidates.sort(key=lambda p: p[0])  # 按 x 排序

        if len(candidates) >= 1:
            result = {
                "leave":   candidates[0],
                "approve": candidates[1] if len(candidates) >= 2 else None,
                "reject":  candidates[2] if len(candidates) >= 3 else None,
            }
            print(f"    [buttons] leave={result['leave']} approve={result['approve']} reject={result['reject']}", flush=True)
            return result

        print(f"    [buttons] 第 {attempt+1} 次取不到，等 2 秒重試...", flush=True)
        time.sleep(2)

    # Fallback：加班/外出申請單預設值
    print("    [buttons] fallback 到預設座標", flush=True)
    return {
        "leave":   COORD["nomad_leave_fb"],
        "approve": COORD["nomad_approve_fb"],
        "reject":  None,
    }


# ════════════════════════════════════════════════════════════════════════════════
# Verse 導航
# ════════════════════════════════════════════════════════════════════════════════

def launch_verse():
    print("  啟動 HCL Verse...", flush=True)
    adb("shell", "am", "start", "-n", "com.lotus.sync.traveler/.LotusTraveler")
    time.sleep(3)


def sync_now():
    """點 ⋮ → Sync Now 觸發伺服器同步"""
    xml = dump_ui()
    # 找 ⋮（More options）按鈕
    mo = re.search(r'content-desc="More options"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if mo:
        cx = (int(mo.group(1)) + int(mo.group(3))) // 2
        cy = (int(mo.group(2)) + int(mo.group(4))) // 2
        adb("shell", "input", "tap", str(cx), str(cy))
    else:
        # fallback：右上角固定位置
        adb("shell", "input", "tap", "2350", "115")
    time.sleep(1)

    # 點 Sync Now
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


def force_sync_unsigned(expected_count, poll_interval=3, timeout=60):
    """
    確保在 Unsigned 資料夾並同步。
    只要有任何信件（>= 1）即繼續；timeout 後不管數量直接處理。
    expected_count=0（遺留檢查模式）時縮短 timeout，沒信件就快速返回（改善 #1）。
    回傳最終取得的 email_list。
    """
    if expected_count == 0:
        timeout = min(timeout, 12)
    print(f"  確保乾淨狀態，導航到 Unsigned（預期新增 {expected_count} 封）...", flush=True)
    ensure_clean_state()
    navigate_to_unsigned()
    time.sleep(1)
    sync_now()
    # sync_now 可能離開 Unsigned，重新導航
    ensure_clean_state()
    navigate_to_unsigned()
    time.sleep(2)

    elapsed = 0
    actual = 0
    while elapsed < timeout:
        email_list = get_email_list()
        actual = len(email_list)
        print(f"  [{elapsed}s] Unsigned 目前 {actual} 封", flush=True)
        if actual >= 1:
            return email_list
        time.sleep(poll_interval)
        elapsed += poll_interval

    print(f"  警告：{timeout}s 後仍沒有信件，繼續嘗試", flush=True)
    return get_email_list()


def _in_unsigned_list(xml=None):
    """
    判斷目前是否在 Unsigned 信件列表頁。
    注意：Folders 資料夾列表頁也含有「Unsigned」文字（資料夾項目），
    必須排除（該頁標題為 Folders）。
    """
    if xml is None:
        xml = dump_ui()
    return ('text="Unsigned"' in xml
            and 'text="Folders"' not in xml
            and ('id/toolbar' in xml or 'content-desc="More options"' in xml))


def _tap_text(xml, text, fallback=None, delay=2):
    """從 dump XML 找指定 text 節點的 bounds 並 tap 中心點；找不到用 fallback 座標"""
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
    """按 Back 2-3 次，確保不在任何開著的信件或子選單內"""
    for _ in range(3):
        if _in_unsigned_list():
            return
        press_back(delay=1.5)
    time.sleep(1)


def navigate_to_unsigned(_depth=0):
    """從 Verse 主畫面或任何畫面導航到 Unsigned 資料夾（資料夾項目動態定位）"""
    if _depth > 4:
        print("  警告：導航到 Unsigned 失敗（重試超過上限）", flush=True)
        return False

    xml = dump_ui()

    # 如果已在 Unsigned 列表，直接回傳
    if _in_unsigned_list(xml):
        print("  已在 Unsigned 資料夾", flush=True)
        return True

    # 如果還停在 Nomad app（表單核准/離開後常見）→ 直接切回 Verse
    if 'package="com.lotus.nomad"' in xml:
        print("  仍在 Nomad app，切回 Verse...", flush=True)
        launch_verse()
        return navigate_to_unsigned(_depth + 1)

    # 如果在開啟的信件檢視頁（Message）→ 按 Back 回列表
    if 'text="Message"' in xml:
        print("  在信件檢視頁，按 Back 回列表...", flush=True)
        press_back(delay=2)
        return navigate_to_unsigned(_depth + 1)

    # 如果在 Folders 資料夾列表頁 → 點 Unsigned（若不在畫面內先往下捲找）
    if 'text="Folders"' in xml:
        if 'text="Unsigned"' in xml:
            print("  在 Folders 列表，點 Unsigned 資料夾...", flush=True)
            _tap_text(xml, "Unsigned", delay=2)
            return navigate_to_unsigned(_depth + 1)
        # Unsigned 在捲動區域外，往下捲最多 3 次再找
        print("  在 Folders 列表，Unsigned 不在畫面內，往下捲找...", flush=True)
        for _ in range(3):
            adb("shell", "input", "swipe", "1200", "800", "1200", "300", "500")
            time.sleep(1)
            xml2 = dump_ui()
            if 'text="Unsigned"' in xml2:
                _tap_text(xml2, "Unsigned", delay=2)
                return navigate_to_unsigned(_depth + 1)
        # 捲完還找不到，fallback 固定座標（前次已在捲底，Unsigned 約在 y=578）
        print("  警告：捲完仍找不到 Unsigned，使用固定座標 (1336, 578)...", flush=True)
        tap(1336, 578, delay=2)
        return navigate_to_unsigned(_depth + 1)

    # 如果在 Verse 主畫面 → 點 Mail
    if 'text="Mail"' in xml:
        print("  從主畫面進入 Mail...", flush=True)
        tap(*COORD["main_mail"], delay=2)
        xml = dump_ui()

    # 開漢堡選單 → Folders → Unsigned（後兩步動態定位）
    if 'text="Inbox"' in xml or 'id/toolbar' in xml:
        print("  開選單 → Folders → Unsigned...", flush=True)
        tap(*COORD["hamburger"], delay=1)
        time.sleep(0.5)
        _tap_text(dump_ui(), "Folders", fallback=COORD["menu_folders"], delay=1.5)
        time.sleep(0.5)
        return navigate_to_unsigned(_depth + 1)

    # 可能正在畫面轉場，先等 2 秒重新判斷，連續失敗才重啟
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
    """
    讀取目前畫面上可見的信件列表。
    回傳 [(cx, cy, subject_text), ...] 按 y 座標排列。
    """
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
        cx = 1268
        cy = (y1 + y2) // 2
        items.append((cx, cy, text))
    items.sort(key=lambda x: x[1])
    return items


def scroll_to_top():
    """捲回列表頂部"""
    adb("shell", "input", "swipe", "1200", "300", "1200", "900", "400")
    time.sleep(1)


def scroll_down():
    """往下捲一頁"""
    adb("shell", "input", "swipe", "1200", "800", "1200", "300", "400")
    time.sleep(1)


def find_next_email(processed):
    """
    在 Unsigned 列表中找第一封尚未處理的信件（支援捲動）。
    processed 是 dict {subject: 已處理封數}，以「出現次數」支援同主旨多封信件（改善 #5）：
    同主旨第 N 封只有在 processed[subject] < N 時才會被選中。
    回傳 (cx, cy, subject_text) 或 None。
    """
    scroll_to_top()
    time.sleep(1.5)  # 等列表完全載入後再讀取
    occurrence = {}   # 本次掃描中各主旨累計出現次數
    prev_texts = []   # 上一頁主旨序列（用於去除捲動重疊）

    for _ in range(10):  # 最多捲 10 次
        items = get_email_list()
        texts = [t for _, _, t in items]

        if prev_texts and texts == prev_texts:
            break  # 捲動後內容不變，已到底部

        # 去除與上一頁重疊的部分，避免同一封信被重複計數
        new_items = items
        if prev_texts:
            max_k = min(len(prev_texts), len(texts))
            for k in range(max_k, 0, -1):
                if prev_texts[-k:] == texts[:k]:
                    new_items = items[k:]
                    break

        for cx, cy, text in new_items:
            occurrence[text] = occurrence.get(text, 0) + 1
            if occurrence[text] > processed.get(text, 0):
                return (cx, cy, text)

        prev_texts = texts
        scroll_down()

    return None


# ════════════════════════════════════════════════════════════════════════════════
# Nomad 表單操作
# ════════════════════════════════════════════════════════════════════════════════

def handle_notes_password_dialog():
    """
    偵測並處理 Notes ID Password 對話框。
    若出現，輸入密碼並點 OK。
    """
    xml = dump_ui()
    if 'Notes ID Password' not in xml and 'Notes ID password' not in xml:
        return False

    print("    偵測到 Notes ID Password 對話框，自動輸入密碼...", flush=True)
    # 找密碼輸入框：EditText 且 password=true 或 hint 含 Password
    pw_match = re.search(
        r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml
    )
    if pw_match:
        cx = (int(pw_match.group(1)) + int(pw_match.group(3))) // 2
        cy = (int(pw_match.group(2)) + int(pw_match.group(4))) // 2
        tap(cx, cy, delay=0.5)
    else:
        tap(1200, 540, delay=0.5)

    # 先清空欄位（全選＋刪除）
    adb("shell", "input", "keyevent", "277")  # KEYCODE_CTRL_A (select all)
    adb("shell", "input", "keyevent", "67")   # KEYCODE_DEL
    time.sleep(0.3)

    # 用 keycode 逐字輸入，避免剪貼簿污染
    DIGIT_KEYCODES = {'0':7,'1':8,'2':9,'3':10,'4':11,'5':12,'6':13,'7':14,'8':15,'9':16}
    for ch in NOTES_PASSWORD:
        kc = DIGIT_KEYCODES.get(ch)
        if kc:
            adb("shell", "input", "keyevent", str(kc))
        else:
            adb("shell", "input", "text", ch)
        time.sleep(0.1)

    # 按 Enter 送出
    adb("shell", "input", "keyevent", "66")
    print("    密碼已送出，等待 Nomad 載入...", flush=True)
    time.sleep(5)
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
    """
    fallback：當 (415,700) 沒打中附件圖示時（純通知信常見，內文是
    "[ 📄 | Link ]" 文字而非真正的附件 icon），改用 uiautomator dump
    找到 "Link" 超連結文字節點並點擊。
    回傳 True 表示找到並點了。
    """
    xml = dump_ui()
    # 找 text="Link" 或 content-desc="Link" 的 clickable 節點
    for m in re.finditer(
        r'(?:text|content-desc)="Link"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml,
    ):
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        print(f"    [fallback] 改點 Link 文字 ({cx}, {cy})", flush=True)
        tap(cx, cy, delay=timeout)
        return True
    return False


def open_nomad_form(email_cx, email_cy):
    """點開信件 → 點附件圖示開啟 Nomad → 每次都確認密碼對話框

    v1.3 新增：開啟 Nomad 後驗證前景 app 確實是 com.lotus.nomad，
    若仍停在 Verse（純通知信點 📄 emoji 沒生效），改點 "Link" 文字節點。
    """
    print(f"    點開信件 ({email_cx}, {email_cy})", flush=True)
    tap(email_cx, email_cy, delay=2.5)
    print(f"    點附件圖示 {COORD['attach_icon']}", flush=True)
    tap(*COORD["attach_icon"], delay=5)
    handle_notes_password_dialog()  # 每次都檢查，有才處理

    # 驗證 Nomad 確實開啟（v1.3：避免通知信只截到 Verse email view）
    pkg = _current_foreground_pkg()
    if "nomad" not in pkg.lower():
        print(f"    ⚠️ Nomad 未開啟（前景：{pkg or '未知'}），嘗試點 Link 文字", flush=True)
        if _try_open_link_text():
            handle_notes_password_dialog()
            pkg = _current_foreground_pkg()
            if "nomad" not in pkg.lower():
                print(f"    ⚠️ Link 點擊後仍未進入 Nomad（前景：{pkg}）", flush=True)
        else:
            print("    ⚠️ 找不到 Link 文字節點，將截到 Verse email view", flush=True)


def read_form_via_screenshot():
    """截圖存到 temp/nomad_form.png，供 AI OCR 讀取表單內容。回傳截圖路徑。"""
    path = screenshot_to_file(os.path.join(_TMP, "nomad_form.png"))
    print(f"    截圖已儲存：{path}", flush=True)
    return path


def _nomad_button_count():
    """
    讀取 Nomad 按鈕列目前的按鈕數（不使用 fallback）。
    3=待核准（離開/核准/駁回）、1=已核准（只剩離開）、0=表單已關閉。
    """
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


def do_approve(buttons):
    """
    執行核准程序：核准 → Comments OK → 遞送 OK
    對話框（Nomad 內部渲染）uiautomator 不一定可見，因此採「容錯按下固定座標 +
    最終語意驗證」：核准完成後按鈕列的核准/駁回按鈕應消失（剩 0 或 1 個按鈕），
    仍偵測到 >= 2 個按鈕則標記 approve_failed（改善 #3）。
    buttons: find_nomad_buttons() 的回傳值
    """
    approve_coord = buttons.get("approve") or COORD["nomad_approve_fb"]
    print(f"    執行核准 {approve_coord}...", flush=True)
    tap(*approve_coord, delay=2)
    tap(*COORD["comments_ok"], delay=2)
    tap(*COORD["delivery_ok"], delay=3)

    # 「遞送完成」對話框可能渲染較慢、固定座標按太早沒按到 → 用 dump 找 OK 補按
    xml = dump_ui()
    if 'text="OK"' in xml:
        print("    偵測到殘留對話框，補按 OK...", flush=True)
        _tap_text(xml, "OK", delay=2)

    # 最終驗證：核准/駁回按鈕應已消失
    for attempt in range(3):
        n = _nomad_button_count()
        if n <= 1:
            print(f"    核准驗證通過（按鈕列剩 {n} 個按鈕）", flush=True)
            return "approved"
        print(f"    [verify] 按鈕列仍有 {n} 個按鈕，等 2 秒再確認...", flush=True)
        time.sleep(2)

    print("    ⚠️ 核准後按鈕列仍有核准按鈕，標記 approve_failed", flush=True)
    return "approve_failed"


def do_leave(buttons):
    """
    已核准或通知，點 Nomad 離開按鈕回到 Verse
    buttons: find_nomad_buttons() 的回傳值
    """
    leave_coord = buttons.get("leave") or COORD["nomad_leave_fb"]
    print(f"    點離開 {leave_coord}...", flush=True)
    tap(*leave_coord, delay=2)
    return "already_approved"


def back_to_unsigned():
    """從 Nomad 表單回到 Unsigned 列表（狀態感知，避免多餘的 press_back）"""
    time.sleep(1)
    # 若已在 Unsigned 列表就不按 Back（Nomad 直接返回 Unsigned 的情況）
    if not _in_unsigned_list():
        press_back(delay=2)
    navigate_to_unsigned()
    time.sleep(2)  # 等 Unsigned 列表完全載入


# ════════════════════════════════════════════════════════════════════════════════
# 主函式（供 hcl_process_all.py 呼叫）
# ════════════════════════════════════════════════════════════════════════════════

def _process_one_email(cx, cy, subject_text, count, review_only):
    """
    處理單封信件：開表單 → 截圖 → 核准/離開。
    回傳 (status, screenshot_path, screenshot_path_b)。
    """
    # 從主旨判斷是待簽核還是通知
    is_notif = "通知" in subject_text and "已核准" not in subject_text and "已批准" not in subject_text

    # 從主旨判斷表單類型 → 取預設按鈕座標
    form_type = get_form_type(subject_text)
    if form_type:
        print(f"    表單類型：{form_type}（使用預設座標）", flush=True)
        preset_buttons = FORM_BUTTONS[form_type]
    else:
        preset_buttons = None

    # 開啟 Nomad 表單
    open_nomad_form(cx, cy)

    # 取按鈕座標
    if preset_buttons:
        buttons = {"leave": preset_buttons["leave"],
                   "approve": None if is_notif else preset_buttons["approve"]}
    else:
        buttons = find_nomad_buttons()

    has_approve = buttons.get("approve") is not None

    # 截圖前先收鍵盤（只在鍵盤確實顯示時才送 BACK，避免誤關表單）
    ime_status = adb("shell", "dumpsys", "input_method")
    if "mInputShown=true" in ime_status:
        adb("shell", "input", "keyevent", "4")
        time.sleep(1.0)

    # 逐頁截圖直到到底，確保完整捕捉事由 / 外出地點 / 日期等所有欄位（改善 #9）
    # v1.3.1：傳入該表單類型的必要欄位 list，每張截圖 OCR 驗證涵蓋率，
    # 收齊或確認到底才停（通知信無 form_type → 不驗證，沿用 hash 邏輯）
    required_fields = FORM_REQUIRED_FIELDS.get(form_type) if not is_notif else None
    screenshots = capture_full_form(count, required_fields=required_fields)

    if review_only:
        # 審查模式：只截圖不核准，離開表單（改善 #8）
        do_leave(buttons)
        status = "reviewed" if has_approve and not is_notif else ("notification" if is_notif else "already_approved")
    elif is_notif or not has_approve:
        status = do_leave(buttons)
        if is_notif:
            status = "notification"
    else:
        status = do_approve(buttons)

    return status, screenshots


def phase2_approve_android(pending_items, ai_judge_fn=None, check_leftover=False, review_only=False):
    """
    Android 版 Phase 2：透過 ADB 操作 Android 模擬器。

    參數：
      pending_items  — Phase 1 回傳的清單 [{category, sender, subject}, ...]
      ai_judge_fn    — 可選，接受截圖路徑並回傳 (has_approve: bool, detail: dict) 的函式
                       若不傳入，預設直接執行核准（不讀取詳細內容）
      check_leftover — Phase 1 掃到 0 筆時仍檢查 Unsigned 是否有遺留信件（改善 #1）
      review_only    — 審查模式：只開表單截圖、不核准，信件留在 Unsigned（改善 #8）

    回傳：results list，格式與舊版相同：
      [{sender, subject, status, detail}, ...]
    """
    print("\n═══ Phase 2 (Android)：核准表單 ═══", flush=True)
    if review_only:
        print("  ⚠️ 審查模式：只截圖、不核准", flush=True)

    pending         = [x for x in pending_items if x.get("category") == "待簽核"]
    notifs          = [x for x in pending_items if x.get("category") == "通知"]
    approved_notifs = [x for x in pending_items if x.get("category") == "核准通知"]

    print(f"  共 {len(pending)} 筆待簽核 / {len(notifs)} 筆通知 / {len(approved_notifs)} 筆核准通知", flush=True)

    results = []

    if pending or notifs or check_leftover:
        expected_count = len(pending) + len(notifs)

        # 清除上次執行遺留的截圖（改善 #7）
        clear_stale_screenshots()

        # 啟動 Verse → 清狀態 → 導航到 Unsigned → 下拉同步 → 確認數量
        launch_verse()
        ensure_clean_state()
        email_list = force_sync_unsigned(expected_count)

        if expected_count == 0 and not email_list:
            print("  Unsigned 沒有遺留信件，跳過 Phase 2", flush=True)
            return results
        if expected_count == 0 and email_list:
            print(f"  ⚠️ 發現 {len(email_list)} 封遺留信件（前次執行未完成），繼續處理", flush=True)

        print(f"  Unsigned 資料夾確認 {len(email_list)} 封信件", flush=True)
        for i, (cx, cy, text) in enumerate(email_list):
            print(f"    [{i+1}] ({cx},{cy}) {text[:40]}", flush=True)

        # ── 逐一處理 Unsigned 裡的信件（動態掃描 + 已處理計數 dict）──────────
        processed = {}  # {subject: 已處理封數}（改善 #5：支援同主旨多封）
        total = max(expected_count, len(email_list))
        count = 0

        while True:
            next_email = find_next_email(processed)
            if not next_email:
                print("  沒有更多未處理信件", flush=True)
                break

            cx, cy, subject_text = next_email
            count += 1
            print(f"\n  [{count}/{total}] ({cx},{cy}) {subject_text[:40]}", flush=True)

            # 單封例外保護：一封失敗不中斷整個流程（改善 #4）
            screenshots = []
            try:
                status, screenshots = _process_one_email(
                    cx, cy, subject_text, count, review_only)
            except Exception as e:
                print(f"    ✗ 處理失敗：{e}", flush=True)
                status = "error"
                try:
                    ensure_clean_state()
                except Exception:
                    pass

            processed[subject_text] = processed.get(subject_text, 0) + 1
            results.append({
                "sender":      "",
                "subject":     subject_text,
                "status":      status,
                "screenshots": screenshots,                          # 完整截圖列表（改善 #9）
                "screenshot":  screenshots[0] if screenshots else None,   # 向後相容
                "screenshot_b": screenshots[-1] if len(screenshots) > 1 else (screenshots[0] if screenshots else None),
            })
            print(f"    → {status}", flush=True)

            try:
                back_to_unsigned()
            except Exception as e:
                print(f"    ⚠️ 返回 Unsigned 失敗：{e}，嘗試重啟 Verse", flush=True)
                launch_verse()
                ensure_clean_state()
                navigate_to_unsigned()
            time.sleep(1)

    # ── 核准通知（不需要 Android 操作）──────────────────────────────────────
    for item in approved_notifs:
        results.append({
            "sender":  item.get("sender", ""),
            "subject": item.get("subject", ""),
            "status":  "approved_notification",
            "detail":  None,
        })

    return results


# ════════════════════════════════════════════════════════════════════════════════
# 單機執行（測試用）
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    review_only = "--review" in sys.argv

    pending = []
    _scan_json = os.path.join(_TMP, "hcl_scan_results.json")
    if os.path.exists(_scan_json):
        with open(_scan_json) as f:
            pending = json.load(f).get("pending", [])

    results = phase2_approve_android(pending, check_leftover=not pending,
                                     review_only=review_only)
    output = {"total": len(results), "results": results}
    with open(os.path.join(_TMP, "hcl_approve_results.json"), "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(output, ensure_ascii=False, indent=2))
