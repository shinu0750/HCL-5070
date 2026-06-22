# verse-to-hindsight-oldmail

批次匯入 HCL Verse 歷史郵件到 Hindsight。以一個月為單位，從最舊的開始跑。

## 使用情境
- 匯入過去特定月份的 Verse 郵件到 Hindsight 知識庫
- 與 `process_one_by_one.py` 串接（分類 + 寫入 Hindsight）
- 不消耗 Claude token（分類用 local Ollama gemma4:e4b）

## 檔案
- `fetch_verse_month.py` — Step 1：抓信（Windows Python + Playwright）
- 分類 + 寫入：使用 `\\wsl.localhost\Ubuntu\home\eid\scripts\email-to-hindsight\process_one_by_one.py`

## 前置需求

```powershell
# Windows Python (3.12)
pip install playwright beautifulsoup4
playwright install msedge

# WSL Python (分類 + 寫入用)
# process_one_by_one.py 已存在，不需額外安裝
```

`~/.hermes/.env` 需包含：
```
HCL_PORTAL_URL=https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp
HCL_VERSE_URL=https://mail1.ecic.com.tw/verse
HCL_USERNAME=shuhsing
HCL_PASSWORD=你的密碼
```

## 使用方式

### Step 1：抓信（Windows PowerShell）

```powershell
# 抓 2026 年 6 月的信件
$env:PYTHONIOENCODING="utf-8"
& "C:\Users\EID\AppData\Local\Programs\Python\Python312\python.exe" `
    ".\.claude\skills\verse-to-hindsight-oldmail\fetch_verse_month.py" `
    --year 2026 --month 6 `
    --output "C:\Users\EID\AppData\Local\Temp\verse_2026_06.json"

# 先預覽（不開信）
... --dry-run

# 只處理前 20 封測試
... --limit 20
```

### Step 2：分類 + 寫入 Hindsight（WSL）

```bash
cd /home/eid/scripts/email-to-hindsight
python process_one_by_one.py --input-json /mnt/c/Users/EID/AppData/Local/Temp/verse_2026_06.json
```

## 輸出格式

JSON 結構與 Gmail 版相同：
```json
[
  {
    "id": "tua0（thread ID）",
    "messages": [
      {
        "id": "UNID",
        "date": "2026-06-21T15:37:08+00:00",
        "subject": "郵件主旨",
        "sender": "sender@email.com",
        "toRecipients": ["to@email.com"],
        "ccRecipients": ["cc@email.com"],
        "attachments": [{"filename": "file.pdf", "mimeType": ""}],
        "plaintextBody": "完整郵件內容..."
      }
    ]
  }
]
```

## 技術說明

### 抓取機制
1. **UNID 清單**：呼叫 Verse pob/api/search/inbox（分頁，每次 50 筆），Python 端過濾目標月份
2. **Body 讀取**：Playwright 開啟 Edge 瀏覽器登入 Verse，對每封信：
   - 用 `[id="{UNID}-msg-info"]` 找到信件 DOM 元素
   - 點擊父 LI 開啟讀信面板
   - 讀取 `.pim-mailread-mailcontent:not(.collapsed-mailcontent)` 的文字
3. **Fallback**：若 body 無法取得（thread 類型信件、行事曆通知等），改用 API 的 `abstract`（約 80-100 字）

### 已知限制
- **歷史信件**（2+ 個月前）：Verse inbox 預設只顯示近期信件，若目標月份很舊，需先在 Verse 手動搜尋該月份（搜尋後信件出現在 DOM，腳本才能點擊）
- **Thread 類型信件**：回覆串中的信件可能只有 abstract（非完整 body）
- **行事曆/系統通知**：可能抓到空 body，會 fallback 到 abstract
- **附件偵測**：只偵測有副檔名的文字，不保證完整

### Verse 特有欄位對應
| Verse 欄位 | Gmail 對應 |
|-----------|-----------|
| `unid`    | message id |
| `tua0`    | thread id  |
| `maildate`| date (ISO 8601) |
| `inetfrom`| sender email |
| `altdisplayname` | sender name |
| `abstract`| 前 100 字 preview |

## 完整月份作業流程

```
最舊月份 → 最新月份（每月一次）

1. 執行 fetch_verse_month.py --year YYYY --month MM
2. 執行 process_one_by_one.py --input-json verse_YYYY_MM.json
3. 確認 Hindsight 已寫入
4. 下一個月
```
