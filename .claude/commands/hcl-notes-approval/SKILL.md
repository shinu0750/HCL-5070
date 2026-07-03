---
name: hcl-notes-approval
description: >
  HCL Notes 表單簽核自動化。當用戶提到簽核、核准、HCL Notes 簽核、
  外出單簽核、加班申請、未刷卡單、待簽核、幫我簽核時使用此 skill。
  此 skill 透過 Playwright 掃描 HCL Verse 收件匣找出待簽核表單，
  再透過 Android 模擬器（ADB）操作 HCL Nomad app 截圖、驗證欄位後核准。
version: 2.0.0
---

# HCL Notes 表單簽核自動化（Android 版）

自動掃描 HCL Verse 收件匣，找出外出單、加班申請、未刷卡單等待簽核表單，
透過 Android 模擬器（ADB）操作 HCL Nomad app 截圖並由 Claude 驗證欄位後核准。

## 必要環境

- **Android 模擬器**：`emulator-5554`，已安裝 HCL Verse（`com.lotus.sync.traveler`）與 HCL Nomad（`com.lotus.nomad`）
- **ADB**：`C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe`
- **Playwright**：Python 套件，用於 HCL Verse 網頁操作（Phase 1 & 3）
- **Python 腳本目錄**：`.claude/commands/hcl-notes-approval/scripts/`
- **環境變數**：`~/.hermes/.env`（含 HCL_USERNAME、HCL_PASSWORD、HCL_PORTAL_URL、HCL_VERSE_URL、HCL_NOTES_PASSWORD）

## 使用方式

用戶指令範例：
- `幫我簽核` → 執行完整流程
- `HCL Notes 有沒有待簽核` → 執行完整流程

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
- 輸出：`hcl_screenshots.json`（`[{subject, screenshots: [paths]}]`）

**retry 模式**（Claude skill 層寫入 `hcl_retry_subjects.json` 後重跑）：
- 若 `hcl_retry_subjects.json` 存在，只重截其中指定的主旨
- 已完成的截圖自動保留，合併輸出

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
- `ok: true` 的信件：開啟 Nomad → 核准（或對通知信點離開）
- `ok: false` 的信件：跳過，status = `screenshot_failed`，保留在 Unsigned
- 輸出：`hcl_approve_results.json`（`{total, results: [{subject, status}]}`）

**Phase 2b status 一覽**

| status | 意義 | 移到 Sign？ |
|--------|------|------------|
| `approved` | 已核准（含驗證） | ✅ |
| `already_approved` | 表單已是核准狀態 | ✅ |
| `notification` | 通知信，點離開 | ✅ |
| `approve_failed` | 核准驗證失敗 | ❌ 留在 Unsigned |
| `screenshot_failed` | 截圖欄位不完整，已跳過 | ❌ 留在 Unsigned |
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

---

### Phase 4：整理結果並寫入 Hindsight（skill 層執行）

從 `hcl_verified.json` 的 `data` 欄位讀取已驗證的欄位資訊，整理成 Markdown 表格：

| 姓名 | 類型 | 日期 | 時間 | 事由 |
|------|------|------|------|------|
| 楊梓盛 | 外出申請 | 2026/07/01 | 12:00–13:00 | 覓食 |

呼叫 `mcp__hindsight__sync_retain`：
- bank：`shuhsing`
- tag：`hcl-approval`、日期
- 內容：表格 + 今日簽核摘要

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
