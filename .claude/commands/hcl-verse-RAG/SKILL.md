---
name: hcl-verse-RAG
description: >
  HCL Verse 信件歸檔 pipeline。當用戶提到歸檔 Verse 信件、處理 04Done 信件、
  把 04Done 的信存成 EML、建立 Verse RAG 索引、整理已完成信件、
  把信移到 domdom、把 Verse 信件上傳到 Gmail 時使用此 skill。從「04Done」資料夾逐封：
  抓全文+附件 → 拆成訊息級 → 建 RAG 索引(Qdrant) + 存成 EML → 移到「domdom」→ 上傳 Gmail(Notes_Import)。
version: 3.0.0
---

# HCL Verse 信件歸檔 Pipeline

從「**04Done**」資料夾逐封處理已完成的信件。討論串（thread）會拆成**訊息級**處理——
每則訊息各自用 Domino UNID 當 `document_id`、各自清乾淨引用歷史再存進 RAG/Hindsight，
避免同一封信被重複歸檔時把舊內容重複灌進去。處理完移到「**domdom**」資料夾。

> 移出 04Done 本身就是「已處理」游標 —— 不需額外記狀態，下次執行不會重複處理。

## 觸發時機

- 「歸檔 Verse 信件」、「處理 04Done」、「整理已完成信件」
- 「把 04Done 的信存成 EML / 建索引」
- 「把信移到 domdom」

## 執行

**先用 `--no-move` 試跑**（只處理頂部第一封、不動信箱），確認抓取/索引正常再正式跑：

```bash
python3 ~/.claude/skills/hcl-verse-RAG/verse_archive_pipeline.py 5 --no-move
```

正式歸檔（會真的把信移到 domdom）：

```bash
python3 ~/.claude/skills/hcl-verse-RAG/verse_archive_pipeline.py [max_results]
```

- `max_results` 處理上限，預設 50
- `--no-move` 只做 EML+RAG、不移動（測試用，且只處理第一封後停）
- `--headful` 顯示瀏覽器視窗（除錯用；瀏覽器一律用 `locale="en-US"` 開，避免 Verse
  跟著系統語系顯示中文介面、對不上寫死的英文 selector）

建議用 Bash `run_in_background: true` 執行，搭配 Monitor 監看
（成功標記：`結果已寫入`；失敗標記：`✗` / `Traceback`）。

**接著自動上傳 Gmail**（正式歸檔後一定要執行，`--no-move` 試跑則略過）：

```bash
python3 ~/.claude/skills/hcl-verse-RAG/verse_upload_gmail.py
```

把 `~/verse-export/` 的 EML 批次 import 到 Gmail 標籤 `Notes_Import`，
成功後搬到 `~/Documents/eml to gamil/eml_done/`。沿用既有 OAuth 憑證/token
（`~/Documents/eml to gamil/`），有自己的 log 去重，重跑只補上次失敗的。

## 流程（每一列信件）

1. 登入 Verse（`locale="en-US"`）→ `open_folder("04Done")` 進指定資料夾
2. 取清單**最上面那封**點開，同時攔截每則訊息展開時打出的 `OpenDocument` 網路請求，
   取得每則訊息在 Domino 資料庫裡的真實 **UNID**（`open_row_and_get_block_unids()`）
3. 抓整串（thread 級）header/raw，供 EML 完整存檔用（不截斷）
4. **逐則訊息**（`extract_message_block()`，跟 Verse 的 accordion 展開順序一一對應）：
   - 沒有完整表頭的（Verse 自己判定內容已被後面訊息的引用完整涵蓋、只給精簡摘要）→ 跳過
   - `clean_body()`：剝 UI chrome 雜訊 → `quote_stripper.strip_quoted_history()` 砍掉引用歷史
     （寄件人:/发件人:/寄件者:/From:+Sent:/-----Original Message-----/-----郵件原件-----/
     Notes 內嵌 `"名字" ---日期---` 等樣式，抓最早出現的位置砍）
   - `resolve_sender()`：用 `email_mapping.py` 查公司通訊錄，把「me」換成目前登入帳號的
     姓名/email（不寫死特定帳號）；查不到的（外部聯絡人/離職同仁）記進未知聯絡人追蹤
   - `document_id` = 這則訊息的 Domino UNID（抓不到才退回 `hash(sender|subject|date)` 備援）
5. 每則訊息各自：
   - **① RAG**：`text-embedding-3-small` → upsert 到 Qdrant collection `verse_emails`
     （payload 含 `subject`/`body`/`from_email`/`from_name`/`to`/`cc`/`date`/`sent_date`/
     `thread_id`/`unid`）
   - **② Hindsight retain**：`retain` 到 `shuhsing` bank，`document_id`=UNID，
     `tags=["source:verse"]`（**proj 分類暫緩**，見下方說明），`content` 用姓名不用 email
6. **③ EML 匯出**：整串完整存檔（不截斷，保留原始信件全貌供人工回溯）→ 下載附件
   （`verify=False`）→ 打包成 `.eml` 存到 `~/verse-export/`
7. **④ 移動**：按「Move to folder」→ 輸入 `domdom` → 該信移出 04Done
8. 那封消失，回到步驟 2 處理下一封最上面的，直到清空或達上限
9. 全部歸檔後：
   - 有新的/更新的未知聯絡人 → 產生/合併 `~/verse-export/external_contacts.xlsx`
     → 發 Google Chat 通知（見「未知聯絡人確認機制」章節）
   - **⑤ 上傳 Gmail**：`verse_upload_gmail.py` 批次 import → 標籤 `Notes_Import` → 搬到 eml_done

**安全閥**：記已處理列的簽章（`hash(subject|sender|snippet)`）；若最上面那列跟上一輪一樣
（代表移動失敗它還在頂部），立即停止，避免無限迴圈或重複索引。這個安全閥是「這一列」層級，
跟訊息級的 `document_id`（UNID）互相獨立。

## proj 分類（暫緩）

歸檔階段先不分類 `proj:`，`tags` 只有 `[source:verse]`。之後補分類時：`document_id`
是可重算的固定值（UNID）、Hindsight `retain` 是 upsert（同 id 直接覆蓋）、`subject`/`body`
已經留在 Qdrant payload 裡 → 寫一支 backfill 腳本可以直接從 Qdrant 撈，不用重新爬 Verse，
兩件事完全解耦。`project_keywords.py` 的 `match_projects()`（多標籤加權）/`match_project()`
（單一，向後相容）都還在，只是目前沒被主流程呼叫。

## 身份解析（email ↔ 姓名）

`email_mapping.py` 查 PostgreSQL 公司通訊錄（`email_mapping` 表，`email` 欄位有 UNIQUE 鍵）：

- `resolve_me(HCL_USERNAME)` → 目前登入帳號的 `(email, 姓名)`，取代寫死特定帳號，
  多帳號（tzuyu/ycmu）代簽時也會抓對
- `email_to_name(email)` / `name_to_email(name)`：雙向查詢
- 主流程的 `resolve_sender(raw)` 回傳 `(email, name, found_in_directory)` 三元組；
  `substitute_me(raw)` 把 to/cc 字串裡的獨立「me」換成目前帳號的 email

## 未知聯絡人確認機制

`email_mapping` 查不到的人（外部廠商/離職同仁不分類，統一判斷條件是「查不到」）：

1. **追蹤**（`external_contacts_tracker.py`）：記進
   `~/.claude/skills/hcl-verse-RAG/external_contacts_state.json`（email → 出現過的顯示名/次數/
   時間範圍/是否已確認）
2. **產生 Excel**（`external_contacts_excel.py`）：只寫還沒確認的列到
   `~/verse-export/external_contacts.xlsx`；重新產生時會保留使用者已填但還沒處理的
   `canonical_name`，不會覆蓋編輯進度
3. **通知**：重用 `hcl-notes-approval/scripts/hcl_write_hindsight.py --notify-only` 機制
   發 Google Chat（依帳號對應不同 space，見 `GOOGLE_CHAT_SPACES`），不重新設計通知管道
4. **讀回確認 → 回填舊資料**（`update_external_contacts.py`，**待實作**）：
   - Upsert 進**同一張** `email_mapping` 表（已確認公司通訊錄同步機制是 upsert，
     不會清掉手動加的列）
   - 用 email 查 Qdrant payload 的 `from_email` 找出所有相關 UNID
   - 重組 Hindsight content（姓名換成確認過的），用**同一個 `document_id`** 重新
     `retain()`（已驗證：同 document_id 重新 retain 會直接覆蓋舊內容/舊抽取事實，
     不會產生重複記錄）；Qdrant 端用 `set_payload()` 同步更新 `from_name`
   - Qdrant 查無資料是正常情況（代表當初 RAG/Hindsight 那步可能失敗過），不是錯誤，
     跳過回填即可

## 結果呈現

讀取兩個結果檔：
- 歸檔：`/tmp/verse_archive_pipeline_result.json`
  `{source, target, no_move, archived_date, sent_date_range, processed, message_total, rag_ok, hindsight_ok, moved, emails[]}`
  （`emails[]` 每筆現在含 `message_count`/`rag_ok`/`hindsight_ok`，因為一列信件可能拆成多則訊息）
- 上傳：`/tmp/verse_upload_gmail_result.json`
  `{total, uploaded, failed, label, done_folder, results[]}`

呈現格式：

```
✓ 從 04Done 歸檔 N 封（共 M 則訊息）→ domdom
  RAG 索引：M 成功 / Hindsight：M 成功
  EML：~/verse-export/（含附件，整串完整存檔）
  Gmail：上傳 N 封到 Notes_Import（搬到 eml_done）
  外部聯絡人待確認：X 位（已發 Google Chat 通知 / 略過）
  [1] [5/29] PharmaSuite 專案週報（3 則訊息, RAG 3/3, Hindsight 3/3, 3 附件, moved, gmail ✓）
  [2] ...
✗ 失敗：列出 rag/eml/hindsight/move/gmail 任一失敗的信件主旨
```

## 寫入 Hindsight

每則訊息在歸檔時自動 retain（不需手動補寫）。關鍵欄位：

- `document_id` = 該則訊息的 Domino UNID（idempotent，重跑/重複歸檔同一封不會重複）
- `timestamp` = `sent_date`（信件真實寄件日，非歸檔日）
- `content` = 清乾淨且砍過引用歷史的內文，寄件者用姓名（`resolve_sender` 解析），
  不預摘要，讓 Hindsight 自行抽取 facts
- `tags` = `[source:verse]`（proj 分類暫緩，見上方章節）
- `metadata` = `{subject, from_email, from_name, to, cc, thread_id, unid, sent_date}`

> `thread_id` 只進 metadata（搜尋折疊用），不扛記憶責任。

## 技術細節（除錯參考）

- 信件清單 selector：`.seq-msg-row`（列文字含 `From / Subject / Message abstract`；討論串多一行 `Count\nN`）
- 閱讀窗格：`.preview-container`；單則訊息容器：`.preview-container [aria-expanded]`
  （比 `.pim-mailread-container` 精準，後者會把摺疊摘要跟完整訊息都算進去、造成重複計數）
- 資料夾導航：左側 `[role="treeitem"]:has-text("04Done")`；Inbox 才有專屬 class `.inbox`
- **訊息 UNID**：每則訊息展開時會打
  `https://mail1.ecic.com.tw/mail/{db}.nsf/0/{UNID}/?OpenDocument&...xhr=1` 請求，
  不限於有附件的訊息（附件連結 `$File/{UNID}/...?OpenElement` 也帶 UNID，但只有部分
  訊息有附件，不能只靠這個）。攔截網路請求逐則展開取得，見
  `open_row_and_get_block_unids()`。`data-folder-name` 屬性是**資料夾**的 UNID，不是
  訊息的，容易搞混
- 引用歷史分隔符（`quote_stripper.py`，拿 04Done 18 個真實討論串驗證過覆蓋率）：
  `寄件人:`/`发件人:`/`寄件者:`（HCL Verse 手機版用字）/`From:`+`Sent:`（外部 Outlook）/
  `-----Original Message-----`/`-----郵件原件-----`/Notes 內嵌 `"名字" ---日期---`
- 移動鈕：`button.action.pim-move-to-folder.icon`（取**可見**的那個）。
  注意資料夾檢視的 action-bar 是 `action-bar collapse-stage-0`，**沒有** `action-tray-populated`
  （那是 Inbox 檢視才有）—— 不能用父層 class 比對，要直接鎖定按鈕本身
- 移動 popup：`div.folder-tray-float.show`，輸入 `input.folder-search-input` 後選
  `[role='treeitem']:visible:has-text('domdom')`（精準比對，避免選錯同名項目）
- 附件連結：`$File/...?OpenElement`（Domino 標準 URL）；下載需 `verify=False`（公司內部憑證）
- 附件命名：`a.innerText` 若為空或裸副檔名(pdf/xlsx...)，改從 URL 的 `FileName=` 取真檔名
- 日期：`.pim-mailread-sentdate` 底下有 `.pimMailShort`（縮寫）跟 `.pimMailLong`（完整
  時間戳）兩個 span，兩者都在 innerText 裡（不是只有畫面顯示的縮寫），取最長那行即可拿到
  完整時間 → `normalize_sent_date()` 正規化成 ISO；缺年份時推算（月份比今天超前 >7 天 → 去年）。
  避開 `[class*="ate"]`（會混進行事曆 widget 雜訊）
- Embedding：長討論串可能超過 8192 token 上限 → `get_embedding()` 用 tiktoken 截斷到 8000 token
- Qdrant：`http://localhost:6333`，collection `verse_emails`，向量 1536 維
- PostgreSQL（`email_mapping` 表 + 之後的 `update_external_contacts.py` 用）：
  host/port/db/user/password 存在 `~/.hermes/.env` 的 `PG_*` 變數
- 仍想語意搜尋已索引的信：`python3 ~/.claude/skills/hcl-verse-RAG/verse_rag_search.py "查詢" [top_k]`（保留在磁碟）
- Gmail 上傳：`verse_upload_gmail.py [eml_folder] [--label] [--done] [--log]`，
  用 Gmail `messages.import_`(`neverMarkSpam`)。OAuth 憑證/token 在 `~/Documents/eml to gamil/`，
  token 過期會自動 refresh（非互動）；`fix_eml_content` 修補缺/重複的 From 欄位；
  log 去重（`verse_upload_log.txt`），重跑只補失敗的

## 查詢已歸檔的信件

使用 `verse_query.py`（另一支腳本）：

```bash
python3 ~/.claude/skills/hcl-verse-RAG/verse_query.py --search "帆宣請款"        # 找信
python3 ~/.claude/skills/hcl-verse-RAG/verse_query.py --reflect "V3F 最新狀態"   # 問答
python3 ~/.claude/skills/hcl-verse-RAG/verse_query.py --model "PharmaSuite/MES"  # 進度摘要
```

## Gmail Backfill（一次性）

將整個 Gmail 信箱 backfill 到 Hindsight + Qdrant：

```bash
python3 ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py             # 全部
python3 ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py --max 100   # 前 100 封測試
python3 ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py --dry-run   # 只印不寫入
python3 ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py --reset     # 清進度從頭來
```

- `document_id` = `hash(from|subject|date)`，idempotent，可重跑/斷點續跑
- 進度記錄在 `~/.claude/skills/hcl-verse-RAG/backfill_progress.json`
- OAuth 憑證/token 在 `~/Documents/eml to gamil/`

> 這支是獨立的一次性 Gmail 信箱 backfill，跟 04Done pipeline 的訊息級 UNID 機制無關，
> 沒有跟著這次升級（仍是整封信一個 document_id）。

## 已知缺口 / 待辦

- `update_external_contacts.py`（讀回 Excel、upsert `email_mapping`、回填 Qdrant/Hindsight）
  還沒寫，「未知聯絡人確認機制」目前只做到通知，讀回還是手動
- `~/.hermes/.env` 還沒有真的 `OPENAI_API_KEY`，導致整支 pipeline 從未真正跑過完整的
  活體端對端驗證（含真的呼叫 embedding API）——目前只驗證到各模組的單元測試層級
- proj 分類 backfill 腳本還沒寫（暫緩中，等實際批次 review 時再做）

## Changelog

- 3.0.0 (2026-07-10): 訊息級拆分 + 引用截斷 + 身份解析 + 未知聯絡人確認機制
  - **Locale bug 修復**：`browser.new_context()` 加 `locale="en-US"`，避免 Verse 跟著
    系統語系顯示中文介面、對不上寫死的英文 selector（曾造成整支 pipeline 完全抓不到任何
    郵件 meta）
  - **檔案編碼損毀修復**：`verse_archive_pipeline.py` 從遷移到 HCL-5070 那次 commit 起
    中文註解編碼損毀、含非法字元，導致整支腳本從未能被 Python 編譯過；已從封存的舊 repo
    撈乾淨版本回來重建
  - **訊息級去重**：討論串不再整串當一個 document 存，改成逐則訊息各自處理，
    `document_id` 改用 Domino UNID（攔截 `OpenDocument` 網路請求取得），不受帳號/資料夾/
    畫面顯示格式影響，天然 idempotent
  - **引用歷史截斷**：新增 `quote_stripper.py`，拿 04Done 18 個真實討論串驗證覆蓋率後
    定案的分隔符規則，避免同一段舊內容透過 email 引用被重複灌進 Hindsight
  - **身份解析**：新增 `email_mapping.py`，接公司通訊錄（PostgreSQL），把「me」動態解析
    成目前登入帳號（取代寫死 shuhsing，支援 tzuyu/ycmu 代簽）；Hindsight content 用姓名
    不用 email
  - **未知聯絡人確認機制**：新增 `external_contacts_tracker.py` / `external_contacts_excel.py`，
    追蹤通訊錄查不到的人、產生 Excel、發 Google Chat 通知（重用 `hcl_write_hindsight.py
    --notify-only`）；讀回確認、回填 Qdrant/Hindsight 的部分（`update_external_contacts.py`）
    設計已確認可行（含 Hindsight 同 document_id 覆蓋的實測驗證），程式碼待實作
  - 詳細設計討論見對話記錄；本次未執行完整活體端對端驗證（缺真的 `OPENAI_API_KEY`）
- 2.4.0 (2026-07-09): 歸檔時暫緩 proj 分類，先求歸檔完成
  - 移除 `verse_archive_pipeline.py` 歸檔迴圈裡的 `match_project()` 呼叫，`retain` 的
    `tags` 先只寫 `[source:verse]`，不再即時判定 `proj:xxx`
  - 原因：想先把全部信件歸檔完，之後再一次性人工 review 分類策略（批次看清單再決定
    proj），避免邊歸檔邊套用可能不準的關鍵字規則
  - 之後補分類的方式：`document_id` 是可重算、非隨機值，且 Hindsight `retain` 以
    document_id upsert（同 id 直接覆蓋）——之後寫一支 backfill 腳本，從 Qdrant
    collection `verse_emails` 的 payload 重算 `subject`/`body` 對應的 proj，用同一個
    `document_id` 再呼叫一次 `retain()` 覆蓋 `tags` 即可，不需要重新歸檔或重新爬 Verse
  - review 節奏/是否要 `match_projects()` 給建議分類等細節，留到實際整理清單時再定
- 2.3.0 (2026-06-12): pipeline 加入 per-email Hindsight retain（步驟③）。
  `project_keywords.py` 升級為加權 scoring + multi-tag（`match_projects`）。
  新增 `verse_query.py`（reflect / model / search 三合一查詢腳本）。
  `verse-query` skill 同步建立。移除舊版 batch-摘要寫法。
- 2.2.0 (2026-06-10): 串接 Gmail 上傳 —— 歸檔後自動執行 `verse_upload_gmail.py`，
  把 `~/verse-export` 的 EML import 到標籤 `Notes_Import`、搬到 eml_done。
  參數化既有上傳腳本（不動原檔），Hindsight metadata 加 `gmail_uploaded`。
- 2.1.0 (2026-06-10): 修正日期擷取（正規化成 ISO `sent_date` + 補年份，避開行事曆雜訊）；
  Hindsight 寫入區分 `archived_date`（歸檔日）與 `sent_date`（寄件日）；
  embedding 加 tiktoken 截斷修正長討論串超過 8192 token 的失敗。
- 2.0.0 (2026-06-10): 重構為單一歸檔 pipeline（04Done → EML+RAG → domdom）。
  新增 `verse_archive_pipeline.py`；移除獨立的索引/匯出模式（合併入 pipeline）；
  搜尋腳本保留供查詢。修正資料夾檢視的移動鈕 selector、附件裸副檔名命名。
- 1.0.0 (2026-06-03): 初始版本（索引 / 搜尋 / 匯出 三模式，測試階段）。
