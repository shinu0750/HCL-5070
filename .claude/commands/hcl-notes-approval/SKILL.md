---
name: hcl-notes-approval
description: >
  HCL Notes 表單簽核自動化。當用戶提到簽核、核准、HCL Notes 簽核、
  外出單簽核、加班申請、未刷卡單、待簽核、幫我簽核時使用此 skill。
  此 skill 透過 Playwright 掃描 HCL Verse 收件匣找出待簽核表單，
  再透過 Android 模擬器（ADB）操作 HCL Nomad app 截圖、驗證欄位後核准。
version: 2.10.0
---

# HCL Notes 表單簽核自動化（Android 版）

自動掃描 HCL Verse 收件匣，找出外出單、加班申請、未刷卡單等待簽核表單，
透過 Android 模擬器（ADB）操作 HCL Nomad app 截圖並由 Claude 驗證欄位後核准。

## 必要環境

- **Android 模擬器**：`emulator-5554`（ShuHsing，你自己的帳號），已安裝 HCL Verse（`com.lotus.sync.traveler`）與 HCL Nomad（`com.lotus.nomad`）
- **ADB**：`C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe`
- **Playwright**：Python 套件，用於 HCL Verse 網頁操作（Phase 1 & 3）
- **Python 腳本目錄**：`.claude/commands/hcl-notes-approval/scripts/`
- **環境變數**：`~/.hermes/.env`（含 HCL_USERNAME、HCL_PASSWORD、HCL_PORTAL_URL、HCL_VERSE_URL、HCL_NOTES_PASSWORD）
- **Hindsight**（Phase 4 用）：自架服務，Windows 端連 `http://localhost:8888`（API）/
  `http://localhost:9999`（Dashboard），無需登入驗證，不需要 MCP server

### 代簽別人帳號（多使用者）

**帳密存放**：仿照 `~/.hermes/.env` 的格式，每個人一個獨立檔案，放在同一個 `~/.hermes/` 目錄下
（不進 git、不動你自己的 `.env`）：

```
~/.hermes/.env           ← 你自己（預設，現有）
~/.hermes/.env.tzuyu     ← 同事帳密（HCL_USERNAME / HCL_PASSWORD / HCL_NOTES_PASSWORD 三行即可，
                            HCL_PORTAL_URL / HCL_VERSE_URL 全公司共用，程式碼裡已有預設值，不用重複寫）
```

**使用者對照表**（AVD/Port 定義見 `android-start` skill 的「已知裝置對照表」，這裡列帳密檔案跟 Phase 5 通知目標）：

| 使用者 | 帳密檔案 | HCL_ADB_SERIAL | Google Chat space（Phase 5 通知用，必填） |
|--------|----------|-----------------|----------------------------------------------|
| 自己（預設） | `~/.hermes/.env` | `emulator-5554` | `--space h2YgpyAAAAE` |
| tzuyu（同事測試） | `~/.hermes/.env.tzuyu` | `emulator-5556` | `--space 8DyTYKAAAAE` |

> `--space` 是模組化設計，**每個使用者都要明確帶自己的 space，包括自己**——n8n 端沒有任何
> 隱式預設值/fallback，沒帶 `--space` 會直接報錯（見下面 Phase 5）。

**⚠️ 重要：環境變數不會跨工具呼叫存活**——這個 harness 每次 Bash/PowerShell 呼叫都是全新的 shell
process，`$env:HCL_ENV_FILE = "..."` 這種設定只在**同一次呼叫**裡有效，分兩次呼叫（先設變數、
再跑腳本）不會生效。所以必須把設變數跟跑腳本寫在同一個 PowerShell 指令裡：

```powershell
$env:HCL_ENV_FILE = "$HOME\.hermes\.env.tzuyu"
$env:HCL_ADB_SERIAL = "emulator-5556"
python hcl_process_all.py --phase1
```

`hcl_process_all.py` / `hcl_approve_android.py` 讀環境變數的路徑都改成
`os.environ.get("HCL_ENV_FILE", "~/.hermes/.env")`，沒設 `HCL_ENV_FILE` 時行為跟以前一樣。

## 使用方式

用戶指令範例：
- `幫我簽核` → 用自己的帳號（`~/.hermes/.env`）執行完整流程
- `HCL Notes 有沒有待簽核` → 同上
- `幫我用 tzuyu 的帳號跑簽核` / `幫同事簽核（tzuyu）` → 查上面「使用者對照表」，
  用對應的 `HCL_ENV_FILE` + `HCL_ADB_SERIAL` 執行完整流程；執行前先確認 tzuyu 模擬器
  已開機（沒開的話先用 `android-start` skill，跟它說「開 tzuyu」）

---

## 完整工作流程

### 關鍵字

```python
APPROVAL_KEYWORDS = ["外出單", "加班申請", "未刷卡單", "外出單通知"]
```

兩支腳本共用，必須保持一致。

---

### Phase 1：掃描收件匣並移到 Unsigned（Playwright）

```bash
python hcl_process_all.py --phase1
```

1. 登入 HCL Verse（PORTAL_URL → VERSE_URL）
2. 逐頁捲動收件匣（兼容 virtual scrolling），收集主旨含 APPROVAL_KEYWORDS 的信件
3. **全部移到 Unsigned 資料夾，不做分類**
4. 輸出：`hcl_scan_results.json`（`{emails: [{sender, subject}]}`）

---

### Phase 2a：截圖（不核准）

```bash
python hcl_approve_android.py --screenshot-only
```

- 逐封開啟 Unsigned 信件 → 點附件圖示開啟 Nomad → 截圖所有頁面 → 離開（不核准）
- 輸出：`hcl_screenshots.json`（`[{subject, screenshots: [paths], page1_hash}]`）
  `page1_hash` 是第一張截圖（表單頂部，含姓名/工號/狀態）的內容 hash，供 Phase 2b
  核准前比對畫面是否真的是同一封信（見下方 Phase 2b 的 `form_mismatch`）

**retry 模式**（Claude skill 層寫入 `hcl_retry_subjects.json` 後重跑）：
- 若 `hcl_retry_subjects.json` 存在，只重截其中指定的主旨
- 已完成的截圖自動保留，合併輸出

> `hcl_retry_subjects.json` 讀寫都統一用 `encoding='utf-8'`（`hcl_approve_android.py` 讀取端
> 已修正為 `open(retry_path, encoding="utf-8")`），Claude skill 層寫入時一律帶
> `encoding='utf-8'` 即可，不用再擔心跟系統預設編碼（cp950）不符的問題。
>
> ⚠️ **每次要對「一整批新信件」跑 `--screenshot-only` 前，必須先刪除 `hcl_retry_subjects.json`**：
> 這個檔案只要還存在（例如上一輪 retry 用剩的），下一次呼叫就會被誤判成 retry 模式，
> 只處理檔案裡指定的少數幾封，其餘新信件會被靜默跳過而不自知
> （2026-07-03 案例：Phase 1 掃到 9 封新信，因為殘留的 retry 檔案只剩 1 個主旨，
> 實際只截圖處理了 1 封，另外 8 封完全沒被觸碰）。
> ```bash
> rm -f "$TEMP/hcl_retry_subjects.json"
> ```

---

### Claude 截圖驗證（skill 層執行）

對每封信件讀取截圖，驗證是否包含以下五個欄位：
- **姓名**、**類型**（外出申請 / 加班申請 / 未刷卡申請 / 外出單通知）、**日期**、**時間**、**事由**

| 驗證結果 | 動作 |
|---------|------|
| 欄位齊全 | 標記 `ok: true` |
| 欄位缺失 | 寫入 `hcl_retry_subjects.json`，重跑 Phase 2a（最多 3 輪） |
| 3 輪仍缺失 | 標記 `ok: false`，跳過並警告用戶 |

驗證完畢後寫入 `hcl_verified.json`：
```json
[{"subject": "...", "ok": true, "data": {"name": "...", "type": "...", "date": "...", "time": "...", "reason": "..."}}]
```

> **注意**：`hcl_verified.json` 必須用 Python 寫入確保 UTF-8 編碼：
> ```python
> import json, os
> data = [...]
> with open(os.path.join(os.environ['TEMP'], 'hcl_verified.json'), 'w', encoding='utf-8') as f:
>     json.dump(data, f, ensure_ascii=False, indent=2)
> ```

---

### Phase 2b：核准（讀取 hcl_verified.json）

```bash
python hcl_approve_android.py --approve
```

- 讀取 `hcl_verified.json`
- `ok: true` 的信件：開啟 Nomad → 先比對畫面 hash 跟 Phase 2a 的 `page1_hash` 是否一致
  → 一致才核准（或對通知信點離開），不一致代表開錯表單，跳過核准只按離開
- `ok: false` 的信件：跳過，status = `screenshot_failed`，保留在 Unsigned
- 輸出：`hcl_approve_results.json`（`{total, results: [{subject, status}]}`）

> **為什麼要多這層 hash 比對**：舊版核准成功與否只看「按鈕列的按鈕數量有沒有減少」，
> 不檢查畫面內容是不是真的對到目標信件。2026-07-06 案例：Nomad 沒有正確切換到下一封
> 信的表單（仍停留在上一封信操作後的殘留畫面），腳本對著錯的畫面點擊，按鈕數量剛好
> 也符合「核准成功」的判定條件，於是被記錄成 `approved`——但目標表單其實從未被真正
> 核准，直到隔天人工檢查才發現還是「簽核中」。現在核准前会先等畫面穩定並比對
> `page1_hash`，不符就直接判定 `form_mismatch`，跳過核准，留在 Unsigned 讓下一輪重試，
> 不會再產生「日誌說核准成功、實際上什麼都沒發生」的假陽性。

**Phase 2b status 一覽**

| status | 意義 | 移到 Sign？ |
|--------|------|------------|
| `approved` | 已核准（含驗證） | ✅ |
| `already_approved` | 表單已是核准狀態 | ✅ |
| `notification` | 通知信，點離開 | ✅ |
| `approve_failed` | 核准驗證失敗 | ❌ 留在 Unsigned |
| `screenshot_failed` | 截圖欄位不完整，已跳過 | ❌ 留在 Unsigned |
| `form_mismatch` | 核准前畫面內容跟 Phase 2a 截圖不符（開錯表單），已跳過核准僅按離開 | ❌ 留在 Unsigned |
| `error` | 處理時發生例外 | ❌ 留在 Unsigned |

---

### Phase 3：移到 Sign（Playwright）

```bash
python hcl_process_all.py --phase3
```

- 讀取 `hcl_approve_results.json`
- 將 `approved / already_approved / notification` 狀態的信件從 Unsigned 移到 Sign
- 其餘保留在 Unsigned

> **注意**：HCL Nomad 核准後，Verse 端可能自動將信件移出 Unsigned，導致 Phase 3 回報「找不到」，此為正常現象。

> ⚠️ **HCL 系統會對同一份文件持續發送多封重複的提醒信，這是正常現象、不是 bug**：
> 同一個主旨（例如「李國訓的加班申請單，請簽核」）在 Unsigned 裡同時存在 2～4+ 份重複副本
> 是常態。核准其中一份就等於核准了底層文件（其他副本會顯示「簽核完成」），但 Phase 3
> 每次只會搜尋並移動「找到的第一份」，不會一次清掉所有重複副本。
>
> 因此 **Phase 3 跑完一次不代表 Unsigned 真的空了**，必須：
> 1. 用 Playwright 重新查詢 Unsigned 目前實際剩幾筆（見下方「確認資料夾真實狀態」）
> 2. 若不是 0 筆，針對剩餘主旨重跑 Phase 2a（截圖）→ 驗證 → Phase 2b（核准，通常顯示已核准只需離開）→ Phase 3
> 3. 重複直到 Playwright 查詢結果確認為 0 筆
>
> 少數情況下同一主旨會反覆冒出「還有一份」持續好幾輪（可能是測試環境定期重新產生提醒信），
> 若同一主旨重試 3～4 輪後仍有殘留、且每次都確認顯示「簽核完成」，可以視為底層文件已核准、
> 停止追這份殘留副本，跟用戶說明即可，不需要無限循環處理。

### 確認資料夾真實狀態：一律用 Playwright，不要看 Android UI

Android 上的 Verse app 畫面**有快取延遲，不能拿來判斷資料夾目前的真實內容**——
實測多次遇到 Android 畫面顯示一堆看似還在 Unsigned 的信件，但用 Playwright 重新登入
查詢，發現伺服器端實際上已經移空或只剩少數幾筆。要確認 Unsigned／Sign 資料夾目前
真正還有哪些信件時，一律用 Playwright 開瀏覽器查詢：

```python
import sys
sys.path.insert(0, r".claude/commands/hcl-notes-approval/scripts")
from playwright.sync_api import sync_playwright
import hcl_process_all as m

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, channel="msedge")
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
    page = ctx.new_page()
    page.set_default_timeout(60000)
    m._login(page)
    page.locator('[role="treeitem"]:has-text("Unsigned")').first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    results = m._scroll_and_collect_all(page)
    print(f"Unsigned 實際還有 {len(results)} 筆")
    browser.close()
```

Android 畫面只用來實際操作核准動作，不用來判斷「還剩幾筆」。

---

### Phase 4：整理結果並寫入 Hindsight（skill 層執行）

從 `hcl_verified.json` 的 `data` 欄位讀取已驗證的欄位資訊。

> ⚠️ **這台機器沒有 Hindsight 的 MCP server**（舊 macOS/Claude-5070 環境有裝，但沒帶過來）。
> Hindsight 本身是自架在 WSL Docker 裡的服務（container: `hindsight`），Windows 端可直接
> 連 `http://localhost:8888`、**不需要登入驗證**。不用等 MCP 補上，直接呼叫 REST API 即可：
>
> ```bash
> python hcl_write_hindsight.py --date 2026-07-03 --items-file items.json
> ```
>
> `hcl_write_hindsight.py`（位於本 skill 的 `scripts/` 目錄）封裝了寫入邏輯，支援兩種模式：
>
> - **單筆模式**（`--content-file`）：整批摘要當一筆 memory，`timestamp` 預設為執行日
> - **多筆模式**（`--items-file`，建議）：JSON 陣列，**每筆各自帶正確的實際發生時間**，
>   讓 Hindsight 的時間軸準確反映每個表單的日期，而不是全部塞在處理當天
>
> `items.json` 範例（每筆 `content` 用自然語言描述，方便 Hindsight 做事實萃取；
> `timestamp` 用 ISO 8601；`tags`/`document_id` 省略時會自動補上共用標籤與內容雜湊 id）：
> ```json
> [
>   {
>     "content": "楊梓盛（工號7922）外出申請已核准：LIMS現場勘查，地點永光二廠，時間 2026/07/02 09:00–17:00。",
>     "timestamp": "2026-07-02T09:00:00"
>   }
> ]
> ```
>
> **⚠️ 效能注意**：Hindsight 每筆 memory 都要跑一次 LLM 事實萃取，實測單筆約 20~30 秒。
> 多筆一次送同步呼叫很容易超過 HTTP timeout，`hcl_write_hindsight.py` 內建用
> `async=true` 送出後輪詢 `/operations` 端點直到完成，不需要自己處理逾時重試。
>
> 寫入目標：`EID` bank（同一個 Hindsight instance 裡還有 `shuhsing` bank，**不要寫錯**——
> 這兩個 bank 語意不同，`EID` 才是 HCL 簽核記錄該去的地方），tags 固定帶 `hcl-approval`
> 與處理日期（例如 `2026-07-03`）。
>
> 如果之後這台機器裝了 Hindsight 的 MCP server，兩種方式（REST API 直連 / MCP 工具）
> 效果等價，可以擇一使用，不需要互相取代。

---

### Phase 5：Hindsight 寫入成功後通知 Google Chat（選用）

Hindsight 全部寫入成功後，把 Phase 4 用的 Markdown 表格摘要發送到使用者的 Google Chat
（透過 Hermes bot 的 1 對 1 DM，只有使用者看得到，不會被其他人看到）。

**`--space` 是必填參數，每個使用者（包括自己）都要明確帶自己的 space ID**（見上面「使用者對照表」）——
這是刻意的模組化設計，n8n 端沒有任何隱式預設值，沒帶 `--space` 直接報錯，不會有「忘了指定結果
誤發到別人那裡」或「不知道實際送去哪個 space」的情況：

```bash
python hcl_write_hindsight.py --date 2026-07-03 --items-file items.json --notify-file summary_table.md --space h2YgpyAAAAE
```

代簽別人帳號時換成對方的 space：

```bash
python hcl_write_hindsight.py --date 2026-07-03 --items-file items.json --notify-file summary_table.md --space 8DyTYKAAAAE
```

`--notify-file` 指向的檔案內容（建議用 Phase 4 那份 Markdown 表格）會當作
`{"text": "...", "space": "spaces/<id>"}` POST 到 n8n workflow **「[HCL] 簽核完成通知 -> Google Chat」**
（workflow id `sP8hjVz2rl5w7IqC`，webhook path `hcl-approval-notify`）：

```
POST http://10.11.1.59:5678/webhook/hcl-approval-notify
Content-Type: application/json

{"text": "<Markdown 表格內容>", "space": "spaces/8DyTYKAAAAE"}
```

n8n 端只有兩個節點：Webhook → Google Chat 節點（`serviceAccount` 認證）。Google Chat 節點的
`spaceId` 是表達式 `={{ $json.body.space }}`——沒有 `||` fallback，收不到 `body.space` 這步會直接
在 Google Chat 那邊失敗。新增第三人時不用再進 n8n 改工作流，只要呼叫端帶對應的 `--space` 就好，
跟 `android-start` 的「已知裝置對照表」是同一種設計思路。文字內容本身仍是原封不動轉發，不做格式轉換。

> **只在 Hindsight 全部寫入成功時才發通知**：`hcl_write_hindsight.py` 會檢查所有
> operations 的狀態，只要有任何一筆不是 `completed`，就跳過 Google Chat 通知並印出警告，
> 避免「明明資料沒存好，卻通知說完成了」的誤導訊息。
>
> Google Chat 通知失敗（例如 n8n 或網路問題）不會讓整個腳本失敗——Hindsight 的資料已經
> 寫入成功，通知只是錦上添花，失敗時印警告訊息即可，不用重試。

---

## Android 操作座標（橫向 rotation=1，邏輯座標 2400×1080）

### Verse 導航
| 操作 | 座標 |
|------|------|
| Verse 主畫面 → Mail | (1268, 275) |
| 漢堡選單 ☰ | (198, 115) |
| 側邊選單 → Folders | (330, 846) |
| Folders → Unsigned（固定） | (1326, 757) |
| 信件列表點開信件 | y 從 uiautomator dump 取，x=1268 |

### 信件內操作
| 操作 | 座標 | 備註 |
|------|------|------|
| 📄 附件圖示 | (415, 700) | WebView 固定位置 |
| Comments OK | (1604, 753) | 固定 |
| 遞送完成 OK | (1871, 660) | 固定 |

### Nomad 按鈕（動態取座標）

`find_nomad_buttons()` 從 uiautomator dump 取 y=[200,310] 的 clickable 節點，按 x 排序：
- 第 1 個 = 離開，第 2 個 = 核准，第 3 個 = 駁回
- 只有 1 個按鈕 → 已核准（`approve=None`）

| 表單類型 | 核准 | 離開 |
|---------|------|------|
| 外出單 / 加班申請 | (447, 252) | (243, 252) |
| 未刷卡 | (538, 252) | (289, 252) |

---

## 技術注意事項

### 全新/剛匯入的裝置：Execution Security Alert 會擋住表單畫面

新裝的 AVD（例如 tzuyu 剛從範本複製、Notes ID 從未在這台裝置上批准過任何 agent 簽章時）
第一次載入某些表單會跳出 Lotus Notes 原生的「Execution Security Alert」對話框
（"A program is trying to execute a potentially dangerous action..."，內容顯示
`Program signed by`），而不是表單內容。`hcl_approve_android.py` 的 `--screenshot-only`
路徑目前**不會偵測、也不會關閉這個對話框**，會把對話框本身當成表單內容連續截好幾張圖，
截圖驗證時会發現這些頁面完全沒有姓名/日期/事由等欄位——這是判斷「卡在 Execution Security
Alert」的訊號，不是欄位真的缺失。

- 用 `--approve` 核准時反而不太受影響：`_approve_one_email` 本身就有「偵測到殘留對話框，
  補按 OK」的 fallback，核准流程會自動點掉它並繼續（見 log 出現
  `偵測到殘留對話框，補按 OK...核准驗證通過`）。
- 但 `--screenshot-only` 沒有這層 fallback，遇到就會整批截圖失敗。目前只能：
  1. 用 ADB 手動開啟該封信、點附件圖示，等對話框出現
  2. 手動截圖確認欄位（不透過腳本自動截圖），或
  3. 直接讓 `--approve` 走一次（它會自動點掉對話框），跳過人工欄位驗證這一步
     （只在已經從別的管道確認過表單內容沒問題時才這樣做）。
- 這個對話框似乎每個 agent 簽章只需要處理一次；同一封信重新開啟後通常就不會再跳出，
  不需要每次都排除。

### 附件圖示 fallback 流程
1. 點 `(415, 700)` → 確認前景是否為 `com.lotus.nomad`
2. 若否 → 找 "Link" 文字節點點擊
3. 若 Chrome 攔截 → 從 address bar 取 URL → 轉換 `notes://` scheme → `am start` 開 Nomad

### 密碼處理
- 偵測到 "Notes ID Password" 對話框才處理，否則跳過
- 密碼用 keycode 逐字輸入（避免剪貼簿污染），值從 `HCL_NOTES_PASSWORD` 讀取

### 移動信件按鈕 selector
```python
"div.action-tray-populated button.action.pim-move-to-folder.icon"
```

---

## 已驗證表單類型

| 表單 | 附件圖示 | 核准按鈕 | 離開按鈕 | 測試日期 |
|------|----------|----------|----------|---------|
| 外出申請單 | (415, 700) ✅ | (447, 252) ✅ | (243, 252) ✅ | 2026-07-03 |
| 加班申請單 | (415, 700) ✅ | (447, 252) ✅ | (243, 252) ✅ | 2026-06-01 |
| 未刷卡申請單 | (415, 700) ✅ | (538, 252) ✅ | (289, 252) ✅ | 2026-06-01 |

---

## Changelog

- 2.10.0 (2026-07-07): 核准前新增畫面內容 hash 比對，防止假陽性 `approved`
  - **事故**：2.9.0 修正的是「按錯按鈕」，但同一晚的執行紀錄還發現另一個更隱蔽的
    問題——穆彥池的外出單核准後隔天複查仍是「簽核中」，日誌卻記錄 `approved`。
    根因是舊版 `do_approve` 判定成功與否只看「按鈕列的按鈕數量有沒有減少」，完全
    不檢查當下畫面是不是真的對到目標信件；一旦 Nomad 沒有正確切換到下一封信的
    表單（例如仍停留在上一封信操作後的殘留畫面），腳本對著錯的畫面按下去，
    按鈕數量的變化剛好也符合判定條件，於是被誤記成功，但目標表單其實從未被
    真正核准
  - **修正**：`phase2a_screenshot_all` 存下每封信第一張截圖的內容 hash
    （`page1_hash`，寫入 `hcl_screenshots.json`）；`_approve_one_email` 核准前
    先呼叫新增的 `_wait_form_loaded()` 等畫面穩定，重新比對 hash，不一致就
    直接判定新狀態 `form_mismatch`（跳過核准、只按離開、留在 Unsigned 供下輪
    重試），不再核准到錯的畫面卻回報成功。`capture_full_form` 內原本重複定義
    的 `content_hash`/載入等待邏輯一併抽成共用的 `_content_hash()` /
    `_wait_form_loaded()`，Phase 2a/2b 共用同一套雜湊邏輯
  - 順手修正 `hcl_screenshots.json` 讀寫沒有明確指定 `encoding="utf-8"` 的問題
    （跟 2.8.0 修正 `hcl_retry_subjects.json` 同類）

- 2.9.0 (2026-07-07): 修正誤觸「已核准通知」畫面按鈕、意外取消外出單的嚴重 bug
  - **事故**：`hcl_approve_android.py` 判斷「是否為純通知信」原本用
    `is_notif = "通知" in subject`，但「XX的外出單已核准」這類通知信主旨不含
    「通知」字面，被誤判成待核准表單，直接套用 `FORM_BUTTONS` 固定座標
    (447,252) 當核准鈕去點。實際畫面在同一位置擺的其實是「外出單取消通知」
    按鈕（已核准通知信跟待簽核表單的按鈕列完全不同），結果誤觸取消，
    系統還真的寄出取消通知信給 HR（收件人 `[F1HR]`），內容是系統罐頭文字
    「我已口頭取得主管同意」——不是使用者本人的陳述
  - **修正**：新增 `_find_text_bounds()`，`_get_buttons_for()` 改為一律先讀
    UI dump 確認畫面上真的有 `text="核准"` 節點才回傳核准鈕座標，找不到就只
    會點「離開」——不再用主旨字串猜測畫面類型、也不再盲信 `FORM_BUTTONS`
    固定座標一定對應「核准」語意。`_get_buttons_for` 移除 `is_notif` 參數，
    Phase 2a/2b 呼叫處同步更新；`_approve_one_email` 的 `is_notif` 只保留用於
    回傳狀態文字（`notification` vs `already_approved`），不再影響按哪個按鈕
  - 這起事故沒有自動回復（HR 已收到取消通知信），需使用者自行決定是否要
    重新提交外出單或跟 HR 說明；本次修正只處理程式邏輯，不動事故本身的資料
- 2.8.0 (2026-07-06): 修正 `hcl_retry_subjects.json` 編碼不一致的 bug，補 tzuyu 帳號實戰踩坑點
  - `hcl_approve_android.py` 讀取 `hcl_retry_subjects.json` 改為明確指定
    `encoding='utf-8'`（原本用系統預設編碼，Windows 中文環境是 cp950），
    修正 Claude skill 層用 UTF-8 寫入時會 `UnicodeDecodeError` 掛掉的問題；
    現在讀寫兩端統一用 UTF-8，不用再遷就系統預設編碼
  - 新增「全新/剛匯入的裝置：Execution Security Alert 會擋住表單畫面」說明：
    tzuyu 這類剛複製的 AVD 第一次開表單會跳出 Lotus Notes 原生簽章授權對話框，
    `--screenshot-only` 沒有處理這個對話框的 fallback，會把對話框截成好幾張「空白」表單圖；
    `--approve` 則因為既有的「殘留對話框補按 OK」邏輯意外地能自動跳過
- 2.7.0 (2026-07-05): `--space` 改為必填，移除 n8n 端隱式 fallback
  - 使用者要求做成純模組：不管是誰（包括自己）都要明確帶 `--space`，不依賴任何預設值
  - n8n Google Chat 節點的 `spaceId` 表達式從 `{{ $json.body.space || 'spaces/h2YgpyAAAAE' }}`
    改成 `{{ $json.body.space }}`，沒收到 `body.space` 會直接在 Google Chat 那步失敗
  - `hcl_write_hindsight.py`：用 `--notify-file` 時若沒帶 `--space` 直接報錯退出；
    `notify_google_chat()` 的 `space` 改為必填參數
  - 「使用者對照表」補上自己的 space `h2YgpyAAAAE`，兩個使用者都要明確帶
- 2.6.0 (2026-07-05): Phase 5 通知支援代簽對象各自的 Google Chat space
  - n8n workflow「[HCL] 簽核完成通知 -> Google Chat」的 Google Chat 節點 `spaceId` 改成表達式
    `{{ $json.body.space || 'spaces/h2YgpyAAAAE' }}`，不用再為每個人複製一份工作流
  - `hcl_write_hindsight.py` 新增 `--space` 參數，帶了就在 POST body 加 `space` 欄位，
    沒帶時維持原行為（fallback 到自己的 space）
  - 「使用者對照表」補上 Google Chat space 欄位；tzuyu 對應 `8DyTYKAAAAE`
- 2.5.0 (2026-07-05): 帳密改用每人獨立檔案 + 修正環境變數用法
  - 新增 `HCL_ENV_FILE` 環境變數：`.env` 讀取路徑改成
    `os.environ.get("HCL_ENV_FILE", "~/.hermes/.env")`，可指向 `~/.hermes/.env.<人名>` 等
    獨立帳密檔案，不用每次口述帳密、也不用動自己的 `.env`
  - 修正前一版的錯誤假設：`$env:...` 設定不會跨 Bash/PowerShell 工具呼叫存活（harness 每次
    呼叫都是全新 shell process），設變數跟跑腳本必須寫在**同一次**指令裡，不能分兩次呼叫
  - 新增「使用者對照表」（帳密檔案 ↔ HCL_ADB_SERIAL），並補上對應的下 prompt 範例
- 2.4.0 (2026-07-05): 支援代簽別人帳號（多 AVD）
  - `hcl_approve_android.py` 的 `SERIAL` 改讀環境變數 `HCL_ADB_SERIAL`（預設仍 `emulator-5554`），
    可指向 tzuyu 等其他固定 port 的測試機，不用改程式碼
  - 帳密（`HCL_USERNAME`/`HCL_PASSWORD`/`HCL_NOTES_PASSWORD`）用 session 環境變數覆蓋即可，
    不用動 `~/.hermes/.env`（兩支腳本本來就用 `setdefault`，session 變數優先）
  - 搭配 `android-start-tzuyu` skill：tzuyu 固定 `-port 5556`，`android-start` 也補上 `-port 5554`，
    避免兩台模擬器搶 port 造成 serial 對錯裝置
- 2.3.0 (2026-07-05): 新增 Phase 5 — Hindsight 寫入成功後通知 Google Chat
  - 新建 n8n workflow「[HCL] 簽核完成通知 -> Google Chat」（id `sP8hjVz2rl5w7IqC`，
    webhook path `hcl-approval-notify`），沿用 Hermes 既有的 Google Chat Service Account
    與 Space，webhook 收到 `{"text": "..."}` 後原封不動轉發，不做格式轉換
  - `hcl_write_hindsight.py` 新增 `--notify-file` 參數：Hindsight 全部寫入成功才發送通知，
    避免資料沒存好卻誤報完成；Google Chat 通知失敗不影響 Hindsight 寫入結果
- 2.2.1 (2026-07-04): 修正 Hindsight 寫入目標 bank
  - 正確的寫入目標是 `EID` bank，不是 `shuhsing`（兩個 bank 語意不同）
  - `hcl_write_hindsight.py` 的 `--bank` 預設值改為 `EID`
  - 修正前已誤寫入 `shuhsing` 的 10~11 筆測試記錄已刪除清乾淨
- 2.2.0 (2026-07-03): Phase 4 改用 REST API 直連 Hindsight，不再依賴 MCP
  - 新增 `hcl_write_hindsight.py`：直接呼叫 `http://localhost:8888` 的 Hindsight REST API
    寫入記憶，取代原本呼叫 `mcp__hindsight__sync_retain`（這台機器沒裝該 MCP server）
  - 支援多筆模式，每筆可帶各自的實際發生 `timestamp`，不會全部塞在處理當天
  - 因單筆 LLM 事實萃取約 20~30 秒，內建 `async=true` + 輪詢 operations 端點，避免逾時
  - 「必要環境」補上 Hindsight 連線資訊
- 2.1.0 (2026-07-03): 根據實戰經驗補三個易踩坑點
  - Phase 2a：明確要求對新一批信件跑 `--screenshot-only` 前先刪除 `hcl_retry_subjects.json`，
    否則會被誤判成 retry 模式，靜默漏掉大部分新信件
  - Phase 3：說明 HCL 系統對同一份文件會重複發送多封提醒信是正常現象，Phase 3 一次只搬
    一份，Unsigned 未必真的清空，需要用 Playwright 重新確認並視情況重跑到真正淨空為止
  - 新增「確認資料夾真實狀態」說明：Android UI 有快取延遲不可信，一律用 Playwright 查詢
- 2.0.0 (2026-07-03): 全面重構
  - Phase 1：移除分類邏輯，APPROVAL_KEYWORDS 符合的信件全移到 Unsigned
  - Phase 2 拆成 2a（截圖）和 2b（核准），中間由 Claude skill 層驗證欄位
  - 欄位驗證：姓名、類型、日期、時間、事由，缺失則重新截圖最多 3 輪
  - 移除 macOS Vision OCR、--review 模式、ai_judge_fn、category 分類邏輯
  - APPROVAL_KEYWORDS 統一為 ["外出單", "加班申請", "未刷卡單", "外出單通知"]
  - hcl_process_all.py：535 → 442 行；hcl_approve_android.py：1204 → 948 行
- 1.3.5 (2026-06-11): 修正 Phase 4 notification 通知信 OCR 漏讀問題
- 1.3.4 (2026-06-10): 修正 _move_email_to_folder 假性成功 bug
- 1.3.3 (2026-06-09): 修正 capture_full_form hash 誤判與座標越界
- 1.3.2 (2026-06-09): Phase 1 兼容 virtual scrolling
- 1.0.0 (2026-06-03): 納入版本管理，初始版本
