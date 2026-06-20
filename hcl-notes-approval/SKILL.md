---
name: hcl-notes-approval
description: >
  HCL Notes 表單簽核自動化。當用戶提到簽核、核准、HCL Notes 簽核、
  外出單簽核、加班申請、未刷卡單、待簽核、幫我簽核時使用此 skill。
  此 skill 透過 Playwright 掃描 HCL Verse 收件匣找出待簽核表單，
  再透過 DroidMind 操作 Android 模擬器上的 HCL Verse / Nomad app 逐一核准表單。
version: 1.3.5
---

# HCL Notes 表單簽核自動化（Android 版）

自動掃描 HCL Verse 收件匣，找出外出單、加班申請、未刷卡單等待簽核表單，
透過 Android 模擬器（DroidMind）操作 HCL Nomad app 逐一核准。

## 必要環境

- **Android 模擬器**：`emulator-5554`，已安裝 HCL Verse（`com.lotus.sync.traveler`）與 HCL Nomad（`com.lotus.nomad`）
- **ADB**：`/Users/shuhsing/Library/Android/sdk/platform-tools/adb`
- **DroidMind MCP**：連線 `emulator-5554`（`~/.claude.json` 設定，PATH 含 `/Users/shuhsing/.local/bin`）
- **Playwright**：Python 套件，用於 HCL Verse 網頁操作（Phase 1 & 3）
- **Python 腳本目錄**：`~/.claude/skills/hcl-notes-approval/scripts/`（隨 skill 一起 git 版控；舊路徑 `/Users/shuhsing/.hermes/scripts/` 留有 symlink 相容）
- **環境變數**：`~/.hermes/.env`（含 HCL_USERNAME、HCL_PASSWORD、HCL_PORTAL_URL、HCL_VERSE_URL）

## 使用方式

用戶指令範例：
- `幫我簽核` → 執行完整流程
- `HCL Notes 有沒有待簽核` → 先掃描，列出清單再詢問是否核准
- `核准所有外出單` → 篩選只核准外出單

## 執行模式與狀態（v1.1.0 新增）

### 審查模式（先看內容再核准）
```bash
python3 ~/.claude/skills/hcl-notes-approval/scripts/hcl_process_all.py --review
```
- Phase 2 只開表單**截圖、不核准**，信件留在 Unsigned（status=`reviewed`）
- Claude 讀取截圖向用戶呈現內容，確認後**再跑一次不帶 flag**：
  Phase 1 掃到 0 筆 → 自動觸發 Unsigned 遺留檢查 → 接手核准
- 用戶說「先給我看內容再核准」「審查後簽核」時使用此模式

### Unsigned 遺留檢查
- Phase 1 掃到 0 筆時**不再直接結束**，會到 Android 端檢查 Unsigned 資料夾
- 若有前次執行中斷的遺留信件，自動接手處理；沒有則快速結束（約 12 秒）

### 核准結果驗證
- 對話框（Nomad 內部渲染）uiautomator **不一定看得到**，不可用對話框偵測當前置條件
- `do_approve()` 採「容錯按下固定座標 + 最終語意驗證」：按完三步後檢查按鈕列，
  核准/駁回按鈕應消失（剩 0~1 個按鈕）；仍有 ≥2 個按鈕 → `approve_failed`
- `approve_failed` / `error` 的信件**留在 Unsigned 不移到 Sign**，下次執行會被遺留檢查接手（自我修復）

### Phase 2 status 一覽
| status | 意義 | 移到 Sign？ |
|--------|------|------------|
| `approved` | 已核准（含驗證） | ✅ |
| `already_approved` | 表單已是核准狀態 | ✅ |
| `notification` | 通知信，點離開 | ✅ |
| `approved_notification` | 核准通知信 | ✅ |
| `reviewed` | 審查模式截圖完成，未核准 | ❌ 留在 Unsigned |
| `approve_failed` | 核准驗證失敗 | ❌ 留在 Unsigned |
| `error` | 處理時發生例外 | ❌ 留在 Unsigned |

> Phase 4 寫入 Hindsight 時，若是直接核准模式（未經內容審查），摘要需註明「未經內容審查即核准」。

---

## 完整工作流程

### 主程式

```bash
python3 ~/.claude/skills/hcl-notes-approval/scripts/hcl_process_all.py
```

四個階段，資料在記憶體中傳遞：
- **Phase 1**（Playwright）：掃描收件匣，移到 Unsigned
- **Phase 2**（Android DroidMind）：逐一開啟 Nomad 表單，核准或離開
- **Phase 3**（Playwright）：將 Unsigned 信件移到 Sign
- **Phase 4**：整理結果，顯示表格，寫入 Hindsight

---

### Phase 1：掃描收件匣並移到 Unsigned（Playwright）

執行：
```bash
python3 ~/.claude/skills/hcl-notes-approval/scripts/hcl_process_all.py  # phase1_scan_and_move()
```

- 登入 HCL Verse（PORTAL_URL → VERSE_URL）
- 搜尋關鍵字：外出單、加班申請、未刷卡單、外出通知
- 捲動收件匣到底部（JS scroll）
- 分類：
  - 主旨含「已核准」或「已批准」→ **核准通知**（不移動）
  - 主旨含「通知」→ **通知**（移到 Unsigned）
  - 其餘 → **待簽核**（移到 Unsigned）
- 將待簽核與通知信件移到 **Unsigned 資料夾**（不再收集 notes:// URL）
- 輸出：`[{category, sender, subject}, ...]`

> ⚠️ 與舊版的差異：舊版收集 notes:// URL 供 cua-driver 使用；新版改為直接移動到 Unsigned，由 Android 端從該資料夾取件。

---

### Phase 2：Android 核准（DroidMind + ADB）

執行：
```bash
python3 ~/.claude/skills/hcl-notes-approval/scripts/hcl_approve_android.py
```

或由 `hcl_process_all.py` 自動呼叫。

**Android 操作座標（橫向 rotation=1，邏輯座標 2400×1080）：**

#### Verse 導航
| 操作 | 座標 |
|------|------|
| Verse 主畫面 → Mail | (1268, 275) |
| 漢堡選單 ☰ | (198, 115) |
| 側邊選單 → Folders | (330, 846) |
| Folders → Unsigned | (1326, 757) |
| 信件列表點開信件 | y 從 uiautomator dump 取，x=1326 |

#### 信件內操作
| 操作 | 座標 | 備註 |
|------|------|------|
| 📄 附件圖示 | (415, 700) | WebView 內，固定座標（WebView bounds [278,489][2382,735]） |

#### Nomad 按鈕（動態取座標）

> ⚠️ **按鈕 x 座標因表單類型而不同**（按鈕文字寬度不同導致右移），**y=252 固定**。
> 腳本使用 `find_nomad_buttons()` 從 uiautomator dump 動態取得，不使用 hardcode。

| 表單類型 | 離開按鈕名稱 | 離開 bounds | 核准 bounds |
|---------|------------|------------|------------|
| 加班/外出申請單 | 離開 | [148,205][339,299] | [352,205][543,299] |
| **未刷卡申請單** | **離開(exit)** | **[148,205][430,299]** | **[443,205][634,299]** |

| 固定按鈕 | Bounds | 中心座標 |
|---------|--------|---------|
| Comments OK | [1533,700][1675,807] | **(1604, 753)** |
| 遞送完成 OK | [1800,607][1942,714] | **(1871, 660)** |

**`find_nomad_buttons()` 邏輯：**
- uiautomator dump 找 y 範圍 [200,310] 且 width < 600 的 clickable 節點
- 按 x 排序：第 1 個=離開，第 2 個=核准，第 3 個=駁回
- 只有 1 個按鈕 → `approve=None`（已核准）；有 3 個 → 待核准
- 取不到時 fallback 到加班/外出申請單預設值

**流程：**
```
啟動 Verse → Sync Now → 確認 Unsigned 信件數 → 逐一處理
for each email (動態掃描，processed dict 依「主旨×出現次數」跳過已處理，支援同主旨多封):
  1. find_next_email(processed) → 取得下一封座標 + 主旨
  2. 從主旨判斷表單類型（加班申請/外出單/未刷卡）
  3. tap 信件 → tap 📄 (415, 700)，等 5 秒
  4. handle_notes_password_dialog() → 若 session 過期自動輸入密碼（keycode 逐字）
  5. keyevent 4 收鍵盤（表單開啟後 comment 欄位 auto-focus 會彈鍵盤蓋住內容），等 1 秒
  6. 截圖上半段存 /tmp/nomad_form_{N}.png
  7. swipe 捲下，截圖下半段存 /tmp/nomad_form_{N}_b.png（含加班歸屬日期等）
  6a. 待簽核 → do_approve(): tap 核准 → Comments OK (1604,753) → 遞送 OK (1871,660)
  6b. 通知   → do_leave(): tap 離開
  7. back_to_unsigned(): press_back → navigate_to_unsigned()
  8. 重複直到 find_next_email 回傳 None
```

**表單類型預設座標（y=252 固定）：**
| 表單類型 | 核准按鈕 | 離開按鈕 |
|---------|---------|---------|
| 加班申請 | (447, 252) | (243, 252) |
| 外出單   | (447, 252) | (243, 252) |
| 未刷卡   | (538, 252) | (289, 252) |

**密碼處理：**
- 每次開 Nomad 後呼叫 `handle_notes_password_dialog()`
- 偵測到 "Notes ID Password" 對話框才處理，否則直接跳過
- 密碼用 keycode 逐字輸入（避免剪貼簿污染），值從 `~/.hermes/.env` 的 `HCL_NOTES_PASSWORD` 讀取（不寫在文件中）
- 輸入前先全選清空（KEYCODE_CTRL_A + KEYCODE_DEL）

**截圖說明：**
- 每封存為 `/tmp/nomad_form_{N}.png`（上半段）與 `/tmp/nomad_form_{N}_b.png`（下半段，含日期）
- 上半段：姓名、工號、狀態、部門、事由開頭
- 下半段：事由完整、**加班歸屬日期**、類別、時數、費用或轉休、直屬主管
- Phase 4 OCR 時需同時讀取兩張截圖

> ⚠️ **鍵盤 auto-focus 問題（已修正）**：Nomad 表單開啟後 comment 欄位自動 focus，彈出鍵盤蓋住整個 WebView。修法：截圖前先 `input keyevent 4` 收鍵盤。

> ⚠️ **WebView 限制**：uiautomator 無法看到 Nomad WebView 內部，按鈕座標靠預設值

---

### Phase 3：移動到 Sign（Playwright）

- 來源：**Unsigned 資料夾**（舊版是收件匣）
- 目的地：**Sign 資料夾**
- 機制：點開信件 → 移動按鈕 → 搜尋「sign」→ 點擊

---

### Phase 4：OCR 讀取表單資訊、呈現結果、寫入 Hindsight

**由 Claude skill 層執行**（hcl_process_all.py 跑完後）：

1. **讀取結果檔**：`/tmp/hcl_process_results.json`
   - `approve` 陣列：每筆有 `subject`、`status`、`screenshot` 路徑

2. **逐張截圖 OCR**：`approve` 陣列中**所有**項目（不論 status）都要 OCR，用 Read tool **同時讀取**：
   - `/tmp/nomad_form_N.png`（上半段：工號、姓名、部門、事由）
   - `/tmp/nomad_form_N_b.png`（下半段：事由完整、**加班歸屬日期**、類別、時數）
   - 從兩張合併提取：姓名、工號、部門、事由、日期、類別、時數、費用或轉休
   - ⚠️ `notification`（通知信）同樣要讀截圖，才能取得外出地點、事由、日期

3. **整理成 Markdown 表格**：

   | 姓名 | 類型 | 日期 | 時間 | 時數 | 事由 |
   |------|------|------|------|------|------|
   | 劉子瑜 | 加班申請 | 2026/05/30 | — | — | kepware憑證更新 |
   | 穆彥池 | 外出申請 | 2026/05/29 | — | — | — |
   | 李安炖 | 外出（通知） | 2026/06/17 | 08:30–17:00 | — | 與富台公司查訪田昌公司 |

   - 類型欄：外出申請、加班申請、未刷卡申請、外出（通知）、核准通知
   - `notification`（通知信）列入表格，類型標「外出（通知）」
   - `approved_notification`（核准通知信）列入表格，類型標「核准通知」
   - 不顯示狀態欄

4. **寫入 Hindsight**：呼叫 `mcp__hindsight__sync_retain`
   - bank：`shuhsing`
   - tag：`hcl-approval`、日期
   - 內容：表格 + 今日簽核摘要

---

## 已驗證表單類型

| 表單 | 附件圖示 | 核准按鈕 | 離開按鈕 | 測試日期 |
|------|----------|----------|----------|---------|
| 加班申請單（OverTime Request） | (415, 700) ✅ | (447, 252) ✅ | (243, 252) ✅ | 2026-06-01 |
| 外出申請單（Absence Request） | (415, 700) ✅ | (447, 252) ✅ | (243, 252) ✅ | 2026-06-01 |
| 未刷卡申請單（Missed Clock-In） | (415, 700) ✅ | (538, 252) ✅ | (289, 252) ✅ | 2026-06-01 |

---

## 技術注意事項

### DroidMind PATH 設定
- 設定檔：`~/.claude.json`（非 `~/.claude/mcp.json`）
- PATH 必須含 `/Users/shuhsing/.local/bin`（uvx 所在路徑）
- 修正後 PATH：`/Users/shuhsing/.local/bin:/Users/shuhsing/Library/Android/sdk/platform-tools:/usr/local/bin:/usr/bin:/bin`

### 裝置資訊
- Serial：`emulator-5554` / Android 14 / sdk_gphone64_arm64
- 物理解析度：1080×2400（直向），操作時 rotation=1（橫向）
- 邏輯座標空間：2400×1080（ADB tap / uiautomator 使用此座標）
- 截圖比例：2400×1080 → ~1536×784（0.64x / 0.726x）

### uiautomator 限制
- 無法看到 WebView 內部（Nomad 表單按鈕、信件內容）
- WebView bounds：`[278,489][2382,735]`（固定，與信件內容多寡無關）
- 附件圖示 y 座標：WebView 第 5 行 ≈ y=700，**不隨信件內容多寡而變**

### 收件匣 JS 捲動
```javascript
const els = [...document.querySelectorAll('*')].filter(el =>
    el.scrollHeight - el.clientHeight > 50 &&
    getComputedStyle(el).overflowY !== 'visible'
);
els.sort((a, b) => b.scrollHeight - a.scrollHeight);
if (els[0]) els[0].scrollTop = els[0].scrollHeight;
else window.scrollTo(0, document.body.scrollHeight);
```

### 移動信件按鈕 selector
```python
# 精確命中可見按鈕（避免多個同名隱藏按鈕）
"div.action-tray-populated button.action.pim-move-to-folder.icon.collapse-stage-0"
# fallback（不帶 collapse-stage-0）
"div.action-tray-populated button.action.pim-move-to-folder.icon"
```

### 備份
原始 cua-driver 版本備份於：`~/Downloads/hcl_scripts_backup_20260531/`

## Changelog

- 1.3.5 (2026-06-11): 修正 Phase 4 說明漏寫 `notification` 通知信也需 OCR — 舊版只提
  `approved_notification` 要列入表格，導致 `notification` 截圖被跳過，通知信的外出地點、
  事由、日期未寫入 Hindsight。修法：(1) Step 2 明確標注「所有 status 都要 OCR」並加 ⚠️ 警示；
  (2) Step 3 表格範例新增 `notification` 列，類型欄補齊全部四種狀態說明。
- 1.3.4 (2026-06-10): `_move_email_to_folder` 同步套用 hcl-move-meeting/construction v1.2.2 的修正 —
  舊版用 `:visible.first` 點下拉項目，過濾沒生效時會選錯項目，造成假性成功（log 報 moved 但實際沒移動）。
  修法：fill("") 清空 + type(delay=50) + `:has-text('{folder_name}')` 明確比對 + popup 關閉驗證。
- 1.3.3 (2026-06-09): 修正 `capture_full_form` 兩個導致截圖卡在頂部的根本原因：
  (1) **hash 誤判**：MD5 對全圖計算，狀態列時鐘每秒變動，導致「hash 不同 = 新一頁」誤判，
  WebView 內容實際沒動。改用 `content_hash()`（PIL 裁掉頂部 50px 狀態列再算 hash），
  只比對表單內容區域。(2) **座標越界**：SCROLL_VARIANTS 三組 swipe 起點 y=800/900/950
  全部落在 WebView 外（WebView 有效 y 範圍 ~310～650），手勢未傳入 WebView。
  改為 `("1200","620","1200","350","400")` / `("1200","650","1200","310","500")` /
  `("1500","640","1500","300","700")`，捲回頂部手勢同步修正為 y=330→630。
  座標驗證方法：透過 DroidMind 直接對 Nomad 表單執行 `input swipe` 並截圖確認。
  新增 `請假單` 到 `APPROVAL_KEYWORDS`（Phase 1 掃描關鍵字）。
  `_scroll_and_collect_all` 停止條件改為 scrollTop 未移動（真正到底），`no_new_limit` 提升為 50。

- 1.3.2 (2026-06-09): Phase 1 收件匣掃描兼容 Verse 的 virtual scrolling — 舊版 `_scroll_to_bottom`
  一次跳到底再用 `locator(...).all()` 收集，但 Verse 只把可見窗 ± buffer 留在 DOM、捲過的會被回收，
  收件匣超過 ~30 封就會漏掉大半（120 封 inbox 可能只抓到前 30 封）。
  改為 `_scroll_and_collect_all`：從頂部開始逐頁往下捲（每頁 85% clientHeight，留 15% 重疊），
  每頁掃描可見 `[role="treeitem"]` 累積去重，連續 3 頁無新主旨 OR 捲到底才停。
  移動階段也改用 `_find_item_by_scroll`：找不到時從頂部逐頁捲找，而非 reload 整頁。
- 1.3.1 (2026-06-09): `capture_full_form` 截圖完當下做「內容涵蓋率驗證」 — 每張截圖用
  macOS Vision framework（pyobjc loadBundle）做中文 OCR，文字累積後檢查是否包含該表單類型的
  所有必要欄位（FORM_REQUIRED_FIELDS：例如外出單要求 工/姓/部門/外出事由/外出地點/外出起訖日期/外出起訖時間）。
  必要欄位全收齊 → 立刻停止捲動；達底部但仍缺欄位 → log 警告。通知信無 form_type → 不驗證。
- 1.3.0 (2026-06-09): 加強截圖完整性，解決 form 4/6 漏截日期時間、form 5/8/9/10 純通知信只截到 Verse email view 的問題：
  - `capture_full_form` Step 3 改為「截圖完當下驗證」：hash 與前一張相同時不直接判定到底，
    改用三段強度遞增的下捲手勢（標準 y=800→400 / 加強 y=900→300 / 最強 x=1500 靠右捲軸 y=950→250）
    逐一重試，三變體都無效才算到底；並要求至少 2 張不同截圖才允許判定到底。
  - `open_nomad_form` 新增前景 app 驗證：點 (415,700) 後若前景不是 `com.lotus.nomad`，
    fallback 用 uiautomator dump 找內文 "Link" 文字節點並點擊（已核准通知信內容是
    `[ 📄 | Link ]` 文字、(415,700) 打不到實際附件 icon）。
- 1.2.3 (2026-06-03): Hindsight sync_retain 指定寫入 shuhsing bank
- 1.2.2 (2026-06-03): 修正 Step 2「捲回頂部」失效 — 表單若一開啟就停在下半段（comment auto-focus 捲到底），原本盲捲 5 次起點 y=200 壓在工具列上（核准按鈕 bounds 到 y=299），滑動沒傳進 WebView，導致只截到下半段（主管核定/守衛填寫/Approval History），漏掉申請人/外出地點/起迄時間。改為 WebView 內容區內手勢（y=450↔820，避開工具列與底部 chip）+ MD5 hash 驗證，連續兩張相同才確認到頂（上限 12 次）；Step 3 下捲起點同步提高到 y=800→400 避開工具列、加大 travel
- 1.2.1 (2026-06-03): 修正截圖過早問題 — `capture_full_form` 重構為明確三步驟：(1) 等載入完成（每 3s 截一張，hash 相同 = 靜止 = 就緒，最多等 30s）(2) 捲回頂部 (3) 逐頁往下截圖直到 hash 相同（= 到底）；3s 間隔確保 spinner 不會轉回同位置誤判；修正前 loading spinner 每張 hash 不同導致 8 張全是載入畫面的 bug
- 1.2.0 (2026-06-03): 改善 #9 — 截圖改為逐頁滾動直到到底，以 MD5 hash 偵測底部，確保完整捕捉事由 / 外出地點 / 日期等欄位；結果 JSON 新增 `screenshots` 清單；Phase 4 改讀全部截圖（不再只讀上半 + 下半兩張）
- 1.1.0 (2026-06-03): 改善 8 項 — (1) 收件匣空時檢查 Unsigned 遺留信件 (2) 文件移除明文密碼 (3) do_approve 逐步驗證對話框、失敗標 approve_failed (4) Phase 2 單封例外保護不中斷流程 (5) 去重 key 改 (sender, subject)、Android 端以出現次數支援同主旨多封 (6) Phase 1 先掃完清單再移動、不再每epoch重載頁面 (7) 執行前清除舊截圖避免 OCR 污染 (8) 新增 --review 審查模式；腳本自 ~/.hermes/scripts 移入 skill 目錄版控（原路徑留 symlink）
- 1.0.0 (2026-06-03): 納入版本管理，初始版本
