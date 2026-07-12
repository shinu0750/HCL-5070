---
name: hcl-verse-contacts-update
description: >
  外部聯絡人名單確認後的回填 pipeline。當用戶說「hindsight聯絡人更新」時使用此
  skill。讀回 hcl-verse-RAG 產生的 external_contacts.xlsx（人工填好 canonical_name
  的列），回填 email_mapping（PostgreSQL）、Qdrant（verse_emails collection 的
  from_name）、Hindsight（EID bank，保留舊 tags/metadata 重新 retain）。
version: 1.1.0
---

# 外部聯絡人回填 Pipeline

跟 hcl-verse-RAG 是同一套資料（同一個 Qdrant collection `verse_emails`、同一個
Hindsight bank `EID`、同一張 `email_mapping` 表），但生命週期不同：hcl-verse-RAG
是「持續在跑、有新信就歸檔」，這支是「主線歸檔完、人工在 Excel 填好正確姓名之後，
回頭修正已經寫進去的資料」的收尾任務，觸發時機跟主線分開，所以獨立成一個 skill。

## 觸發時機

- 「hindsight聯絡人更新」

## 前置條件

`~/verse-export/external_contacts.xlsx` 裡至少有一列 `canonical_name` 欄位填了值
（這個 Excel 由 hcl-verse-RAG 的 `external_contacts_excel.py` 產生，欄位是
`email`/`seen_names`/`count`/`first_seen`/`last_seen`/`canonical_name`）。

## 執行

```bash
python .claude/commands/hcl-verse-contacts-update/update_external_contacts.py
```

- 預設讀 `~/verse-export/external_contacts.xlsx`，可傳自訂路徑當第一個參數
- 只處理 `canonical_name` 有填值的列，空白的列略過（下次還會出現在 Excel 裡）

## 流程

對 Excel 裡每一列已填 `canonical_name` 的聯絡人：

1. **`upsert_email_mapping(email, name)`**：`INSERT ... ON CONFLICT (email) DO
   UPDATE SET name = EXCLUDED.name`，upsert 進 `email_mapping` 表，不會清掉手動
   加的其他列
2. **在 Qdrant 找相關 UNID**：用 `qdrant.scroll()` + `Filter(from_email=email)`
   查 `verse_emails` collection，撈出這個 email 寄出的所有訊息（已實測驗證這個
   filter 查詢方式可行）。查無資料是正常情況（代表當初 RAG 那步可能失敗過），
   跳過那個聯絡人的回填即可，不是錯誤
3. **對每個 UNID**：
   - Qdrant：`set_payload({"from_name": canonical_name})`，只更新這個欄位
   - Hindsight：**先 `get_document(document_id=unid, bank_id="EID")` 讀回舊
     `tags`/`document_metadata`**——這一步不能省，`retain()` 是整段覆蓋不是
     merge，如果沒讀回舊值直接帶新 content 重新 retain，`tags`（目前是
     `["mail"]`，hcl-verse-RAG 3.10.0 加回來給 `reflect()`/`recall()` 過濾用）
     跟 `metadata` 裡的 `thread_id`/`reply_to_unid` 這些欄位會直接消失
   - 把讀回的 `document_metadata` 合併新的 `from_name`/`from_email`，重新組
     `content`（主旨/寄件者/日期/內文），帶著原本的 `tags` 一起重新 `retain()`
   - Hindsight 查無這個 UNID 一樣是正常情況（代表當初 retain 那步可能失敗過），
     跳過即可
4. 全部處理完，把 `external_contacts_state.json` 裡對應的 email 標成
   `confirmed=true`（reuse hcl-verse-RAG 的 `external_contacts_tracker.py`，不
   重複實作 state 檔案讀寫）——下次 hcl-verse-RAG 產生 Excel 時這幾位就不會再
   列出來
5. **把處理完的列直接從 Excel 刪掉**（`ws.delete_rows()`，由後往前刪避免
   index 跟著位移，刪完 `wb.save()` 存回原檔）——`email_mapping` 已經 upsert
   進去了，這列留著沒有意義；更重要的是避免同一份 Excel 沒重新產生就重跑時
   被整批重複處理一次（upsert 沒差，但 Qdrant 全表相關查詢、Hindsight 重新
   `retain()` 都是白做工）

## 技術細節

- **共用 hcl-verse-RAG 的模組**：`sys.path.insert()` 指到 `../hcl-verse-RAG`，
  import 它的 `external_contacts_tracker.load_state()`/`save_state()`（state
  檔案本身也還是放在 `hcl-verse-RAG/external_contacts_state.json`，這支只是
  借用讀寫邏輯，不搬家）。`HindsightClient` 類別另外寫一份（多了
  `get_document()` 方法），沒有直接 import hcl-verse-RAG 那份，因為那份只有
  `retain()`
- **`email_mapping` 表結構**：`id serial pk, name varchar not null, email
  varchar not null unique`，upsert 靠 `email` 的 UNIQUE 鍵
- **`get_document()` 回傳結構**（已實測確認）：`{id, bank_id, original_text,
  tags, document_metadata, retain_params, ...}`——`tags`/`document_metadata`
  就是這支腳本要讀回、合併、重新送出的兩個欄位
- 這支不動 EML/Gmail（分支 B），只動 RAG/Hindsight（分支 A）的資料，因為姓名
  顯示是 RAG/Hindsight 的呈現問題，EML 檔案本身內容不受影響（已經是最終存檔，
  不會回頭改）
