# gmail-to-hindsight-oldmail

批次匯入 Gmail 歷史郵件到 Hindsight。以一個月為單位，從最舊的開始跑。

## 使用情境
- 匯入過去特定月份的 Gmail 郵件到 Hindsight 知識庫
- 與 `process_one_by_one.py` 串接（分類 + 寫入 Hindsight）
- 不消耗 Claude token（分類用 local Ollama gemma4:e4b）

## 檔案
- `fetch_gmail_month.py` — Step 1：抓信（Windows Python + Gmail API）
- 分類 + 寫入：使用 `\\wsl.localhost\Ubuntu\home\eid\scripts\email-to-hindsight\process_one_by_one.py`

## 前置需求

```powershell
# Windows Python (3.12)
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

憑證檔案：
- `C:\Users\EID\Documents\Claude\ShuHsing\credentials.json`（已存在）
- `C:\Users\EID\Documents\Claude\ShuHsing\token.json`（第一次執行時自動產生）

**第一次執行**：會自動開啟瀏覽器，用 shuhsing@ecic.com.tw 帳號授權，完成後 token.json 自動儲存，之後不需再授權。

## 使用方式

### Step 1：抓信（Windows PowerShell）

```powershell
# 抓 2026 年 6 月的信件
$env:PYTHONIOENCODING="utf-8"
& "C:\Users\EID\AppData\Local\Programs\Python\Python312\python.exe" `
    ".\.claude\skills\gmail-to-hindsight-oldmail\fetch_gmail_month.py" `
    --year 2026 --month 6

# 先預覽（只列 thread ID，不讀內文）
... --dry-run

# 只處理前 20 個 thread 測試
... --limit 20

# 自訂輸出路徑
... --output "C:\Users\EID\AppData\Local\Temp\gmail_2026_06.json"
```

預設輸出路徑：`C:\Users\EID\AppData\Local\Temp\gmail_{year}_{month:02d}.json`

### Step 2：分類 + 寫入 Hindsight（WSL）

```bash
cd /home/eid/scripts/email-to-hindsight
python process_one_by_one.py \
  --input-json /mnt/c/Users/EID/AppData/Local/Temp/gmail_2026_06.json
```

## 輸出格式

與 Verse 版相同（`process_one_by_one.py` 共用格式）：

```json
[
  {
    "id": "thread_id（Gmail hex）",
    "messages": [
      {
        "id": "message_id（Gmail hex）",
        "date": "2026-06-21T15:37:08+00:00",
        "subject": "郵件主旨",
        "sender": "sender@email.com",
        "toRecipients": ["to@email.com"],
        "ccRecipients": ["cc@email.com"],
        "attachments": [{"filename": "file.pdf", "mimeType": "application/pdf"}],
        "plaintextBody": "完整郵件內容..."
      }
    ]
  }
]
```

## 完整月份作業流程

```
最舊月份 → 最新月份（每月一次）

1. 執行 fetch_gmail_month.py --year YYYY --month MM
2. 執行 process_one_by_one.py --input-json gmail_YYYY_MM.json
3. 確認 Hindsight 已寫入
4. 下一個月
```

## 技術說明

- Gmail API 查詢：`after:YYYY/MM/01 before:YYYY/MM+1/01`
- 抓取單位：thread（同一串對話算一筆）
- body：優先取 `text/plain` part，若無則 fallback 到 `snippet`（約 100 字）
- 附件偵測：只記錄 `Content-Disposition: attachment` 的真實附件
- OAuth scope：`gmail.readonly`（唯讀，不會修改信箱）
