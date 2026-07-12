---
name: hcl-verse-contacts-update
description: >
  外部聯絡人名單確認後的回填 pipeline。當用戶說「hindsight聯絡人更新」時使用此
  skill。讀回 hcl-verse-RAG 產生的 external_contacts.xlsx（人工填好 canonical_name
  的列），回填 email_mapping（PostgreSQL）、Qdrant（verse_emails collection 的
  from_name，以及 to/cc 欄位裡出現過的舊顯示名）、Hindsight（EID bank，保留舊
  tags/metadata 重新 retain）。
version: 2.0.0
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
2. **`backfill_contact()`：單一聯絡人的完整回填，只全表掃一次**——`to`/`cc`
   欄位裡沒有 `to_email`/`cc_email` 這種結構化欄位可以查，本來就要全表
   `scroll()`（不帶 filter，用 `next_page_offset` 分頁掃完整個 collection）
   比對文字；既然反正要掃全表，`from_email` 命中的判斷也一起在同一輪掃描裡做，
   不再用 `Filter(from_email=email)` 另外精準查一次：
   - 對每個 point，同時檢查兩個條件：
     - `from_email == email` -> 這筆是這個聯絡人寄的，`payload_update["from_name"]`
       + `metadata_update["from_name"/"from_email"]`
     - `to`/`cc` 存的是 `resolve_recipients()` 組完的『、』分隔姓名字串，跟
       `external_contacts_state.json` 裡這個聯絡人的 `seen_names`（被確認前
       Verse 顯示過的所有舊名字）逐段完全比對（不是子字串比對，避免誤傷剛好
       名字部分重疊的其他字串），命中就 `payload_update["to"/"cc"]` +
       `metadata_update["to"]`（Hindsight `metadata` 本來就沒有 `cc`，3.7.0 移除）
   - 兩個條件都沒命中就整筆跳過，不呼叫 Qdrant/Hindsight
   - 命中任一個：Qdrant `set_payload(payload_update)`**一次**更新所有變動欄位；
     Hindsight **先 `get_document(document_id=unid, bank_id="EID")` 讀回舊
     `tags`/`document_metadata`**（這一步不能省，`retain()` 是整段覆蓋不是
     merge，沒讀回舊值直接重新 retain，`tags`——目前是 `["mail"]`，
     hcl-verse-RAG 3.10.0 加回來給 `reflect()`/`recall()` 過濾用——跟
     `thread_id`/`reply_to_unid` 這些欄位會直接消失），合併 `metadata_update`
     後**一次**重新 `retain()`。`from_name` 有變才需要重組 `content`（寄件者
     顯示在 content 裡，主旨/寄件者/日期/內文樣式）；只有 `to` 變的話 content
     不用動，直接沿用 `get_document()` 讀回的 `original_text`
   - Qdrant/Hindsight 查無資料都是正常情況（代表當初那步可能失敗過），跳過即可
   - **為什麼合併成一次而不是分開兩支函式**：原本 `backfill_one()`（精準查
     `from_email`）+ `backfill_to_cc()`（全表掃描）分開各自掃一輪，後者反正要
     掃全表，前者等於白做；更重要的是如果同一筆記錄剛好同時命中兩邊條件
     （例如某人剛好也把自己列在收件人），分開呼叫會對同一個 UNID 各自獨立呼叫
     一次 Hindsight `get_document()`+`retain()`，兩次非同步寫入互相競爭，晚到
     的那次會覆蓋掉先到的那次剛更新的欄位。合併後每筆記錄只組一次
     `metadata_update` 一起送出，這個問題不會發生——已用假資料模擬「同一筆記錄
     同時符合兩邊條件」的邊界情況驗證過，`tags`/`thread_id` 等舊欄位跟兩邊的
     新值都正確合併進同一次 `retain()`
   - 已用真實案例驗證：unid `631D18D31073918548258E21002F841F`（穆彥池寄給
     helenaf 那則），`to` 從 Verse 顯示的英文帳號名「helenaf」正確換成確認過的
     「于宗仁」，同一次全表掃描共找到 6 筆相關記錄，`tags`/`thread_id`/
     `from_name`/`from_email` 等其他欄位完整保留
3. 全部處理完，把 `external_contacts_state.json` 裡對應的 email 標成
   `confirmed=true`（reuse hcl-verse-RAG 的 `external_contacts_tracker.py`，不
   重複實作 state 檔案讀寫）——下次 hcl-verse-RAG 產生 Excel 時這幾位就不會再
   列出來
4. **把處理完的列直接從 Excel 刪掉**（`ws.delete_rows()`，由後往前刪避免
   index 跟著位移，刪完 `wb.save()` 存回原檔）——`email_mapping` 已經 upsert
   進去了，這列留著沒有意義；更重要的是避免同一份 Excel 沒重新產生就重跑時
   被整批重複處理一次（upsert 沒差，但步驟 2 的全表掃描是白做工——一個聯絡人
   只會在被確認的這一次做，不會因為 Excel 沒清乾淨而重複掃）

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
