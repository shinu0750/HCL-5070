讀取會議紀錄或報價單 PDF（手動指定路徑，不是走 hcl-verse-RAG 信件附件的自動 pipeline），
直接抽取欄位、判斷類型、寫入 Hindsight，不逐筆等使用者確認內容。目的地固定是 Hindsight，
不用先跑本機 RAG-Anything（見 `meeting_notes_hindsight_workflow` 這則 memory 的決策原因）。

## 使用方式

```
/meeting-minutes-to-hindsight <PDF路徑或資料夾路徑>
```

## Step 0：先問使用者這次要用哪個 proj tag

**開始處理前一定要先問**，不要用檔名或業務編號自己猜：「這批要寫入 Hindsight 的 `proj:` tag
要用哪一個專案？」拿到答案後，這個值固定套用到這次處理的所有檔案（同一批不用每個檔案重問）。
格式跟 `hcl-verse-RAG` SKILL.md 裡 `VERSE_PROJ_TAG` 的既有慣例一致，用底線不用空白（例如
`四廠JSR_B棟HVM產線建置`）。

## Step 1：讀取 PDF、判斷文件類型

用 Read 工具直接讀（原生支援 PDF）。判斷這份文件是：

- **會議記錄**：長得像客戶名稱/工程名稱/業務編號/會議主題/會議日期/會議地點/主席/記錄/
  出席人員/項次表格（每項有討論及決議事項＋執行單位＋完成期限）
- **報價單**：長得像見積番号/報價單號、廠商名稱、品名/型號/規格（容量、材質、關鍵尺寸）、
  金額或交期
- **其他**（圖面、PID、CAD 等）：跳過，不寫入 Hindsight，記錄到最後的彙總報告裡標示「已略過」

## Step 2：抽取欄位、組 Hindsight item

**會議記錄**：
- `document_id` = `meeting-{文件編號}`（例如 `meeting-C2448-FT-ECIC-M003`），沒有明確文件編號
  時退回 `meeting-{檔名 md5 前12碼}`
- `timestamp` = 會議日期＋開始時間，ISO8601（例如 `2026-01-16T09:30:00`）
- `tags` = `["proj:{proj值}", "會議記錄"]`
- `content` = 工程名稱/業務編號/會議主題/日期地點、主席/記錄/出席人員（業主/廠商分開列）、
  決議事項分兩類：Info（備查，直接列）、待辦事項（帶 `[執行單位:X,期限:Y]`）

**報價單**：
- `document_id` = `quote-{報價單號或見積番号}`，沒有明確編號時退回
  `quote-{檔名 md5 前12碼}`
- `timestamp` = 報價日期（無明確日期時用文件裡最早出現的日期，都沒有則省略此欄位）
- `tags` = `["proj:{proj值}", "報價單"]`
- `content` = 廠商名稱、品名/型號/規格重點（容量、材質、關鍵尺寸）、金額（如有揭露）、
  交期/有效期限（如有）

## Step 3：直接寫入 Hindsight（不等使用者逐筆確認）

`bank_id` 固定 `EID`。把 payload 存成暫存 JSON（放 scratchpad 目錄，不要用 here-string 直接
傳字串，避免長內容/中文編碼在命令列傳遞時出錯）：

```json
{
  "items": [
    {"content": "...", "timestamp": "2026-01-16T09:30:00", "tags": ["proj:C2448", "會議記錄"], "document_id": "meeting-C2448-FT-ECIC-M003"}
  ],
  "async": true
}
```

POST：

```bash
curl -s -X POST http://localhost:8888/v1/default/banks/EID/memories \
  -H "Content-Type: application/json" \
  --data-binary @"<暫存json路徑>"
```

回傳會帶 `operation_id`。輪詢直到完成：

```bash
curl -s http://localhost:8888/v1/default/banks/EID/operations/<operation_id>
```

**實測耐心提醒**：官方文件寫單筆約 20-30 秒，但實測跑過 1-2 分鐘才轉成 `completed` 是正常的
（LLM 事實萃取本身較慢），不要看到還在 `pending` 就以為卡住或失敗，用迴圈每 5-10 秒 poll 一次，
撐到 2-3 分鐘再判斷異常。多筆檔案可以先把所有 POST 都送出去拿到各自的 `operation_id`，
再統一輪詢，不用一筆等完才送下一筆。

## Step 4：搬移歸檔

Hindsight 寫入成功（`completed`）後，把來源 PDF 連同同檔名的 `.json`（如果存在，例如
`{unid}_原始檔名.json` 這種 sidecar）一起搬到對應資料夾：

- 會議記錄 → `\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\meeting minutes\meeting minutes`
- 報價單 → `\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\meeting minutes\quotation`

只搬移，不用改檔名。**Hindsight 寫入失敗（`failed`）的檔案不要搬**，留在原地方便之後重跑。
Step 2 判斷為「其他/略過」的檔案也不搬，維持原樣。

## Step 5：彙總回報

處理完所有檔案後列表回報：檔名、判斷類型（會議記錄/報價單/略過）、`document_id`、
Hindsight 寫入狀態（completed/failed）、抽取出幾筆事實單元（`result_metadata.unit_ids_count`）、
搬移結果（成功搬到哪個資料夾/失敗未搬/略過未搬）。

## 版本記錄

- 1.4.0 (2026-07-14): 新增 Step 4「搬移歸檔」——Hindsight 寫入成功後，把 PDF＋同檔名 json
  sidecar 依類型搬到 `meeting minutes\meeting minutes`（會議記錄）或
  `meeting minutes\quotation`（報價單）；寫入失敗或判定為略過的檔案不搬。
- 1.3.0 (2026-07-14): 拿掉「跟 hcl-verse-RAG 既有分支的差異說明」章節，不需要。
- 1.2.0 (2026-07-14): 拿掉「資料夾是否有自動 pipeline 在跑」的前置檢查——不用做這個判斷，
  直接處理使用者指定的檔案。
- 1.1.0 (2026-07-14): 拿掉逐筆使用者確認步驟（讀取分類抽取後直接寫入），改成一開始就先問
  `proj:` tag 要用哪個專案；擴大範圍支援報價單（`tags` 依類型分別是 `會議記錄`/`報價單`）。
- 1.0.0 (2026-07-14): 首版（單純會議紀錄、逐筆確認後才寫入）。
