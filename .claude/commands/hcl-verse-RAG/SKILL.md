---
name: hcl-verse-RAG
description: >
  HCL Verse 信件歸檔 pipeline。當用戶提到歸檔 Verse 信件、處理 04Done 信件、
  把 04Done 的信存成 EML、建立 Verse RAG 索引、整理已完成信件、
  把信移到 domdom、把 Verse 信件上傳到 Gmail 時使用此 skill。從「04Done」資料夾逐封：
  抓全文+附件 → 拆成訊息級 → 分兩條分支處理（① RAG/Hindsight ② EML/Gmail）→
  移到「domdom」→ 上傳 Gmail(Notes_Import_v2)。
version: 3.15.0
---

# HCL Verse 信件歸檔 Pipeline

從「**04Done**」資料夾逐封處理已完成的信件（來源/目標資料夾、proj tag 皆可用環境變數
覆寫，見「執行」章節，測試其他分類好的資料夾時不用改預設值）。討論串（thread）拆成
**訊息級**處理，每則訊息各自用 Domino UNID 當 `document_id`，然後分成**兩條互相獨立
的分支**，同一份原始內容各自加工成不同用途、互不干擾：

- **分支 A：RAG + Hindsight** —— body 用清完版（`quote_stripper` 砍掉引用歷史），
  避免同一封信被重複歸檔、或討論串裡的舊內容透過引用重複灌入。
- **分支 B：EML + Gmail** —— 每則訊息各自一個 `.eml`（不是一個討論串一個），內文
  用 `eml_body`（只剝 Verse 自己的 UI chrome 雜訊，**不砍引用歷史**，保留信件原貌）。

> **不記錄 `thread_id`/`reply_to_unid`**（3.14.0 拿掉，見 Changelog）：這個 pipeline
> 是每天執行的，同一討論串較早的訊息通常前幾天就已經歸檔並移出 Verse，當下這一批
> 根本看不到完整討論串，硬要配對只會得到不完整、誤導性的關聯。分支 B 因此也不組
> `In-Reply-To`/`References`，Gmail 裡每則訊息都是獨立的信，不會自動合併討論串。
> 之後如果真的需要回覆關聯，應該是後處理（從已存進去的資料反查），不是歸檔當下猜。

處理完移到「**domdom**」資料夾。

> 移出 04Done 本身就是「已處理」游標 —— 不需額外記狀態，下次執行不會重複處理。

## 觸發時機

- 「歸檔 Verse 信件」、「處理 04Done」、「整理已完成信件」
- 「把 04Done 的信存成 EML / 建索引」
- 「把信移到 domdom」

## 執行

> **`python3` vs `python`**：這台機器上 `python3` 是壞掉的 Windows Store 別名
> （靜默失敗、exit code 49，不會有任何錯誤訊息），實際能用的直譯器是 `python`
> （`C:\Users\...\Programs\Python\Python312\python.exe`）。下面指令都用 `python`；
> 如果在真正的 WSL/Linux 環境下執行，`python3` 才是正常對應的指令，屆時可以換回來。

**先用 `--no-move` 試跑**（只處理頂部第一封、不動信箱），確認抓取/索引正常再正式跑：

```bash
python ~/.claude/skills/hcl-verse-RAG/verse_archive_pipeline.py 5 --no-move
```

正式歸檔（會真的把信移到 domdom）：

```bash
python ~/.claude/skills/hcl-verse-RAG/verse_archive_pipeline.py [max_results]
```

- `max_results` 處理上限，預設 50（預設是「信件/列數」，一個討論串算 1 封，不是訊息數）
- `--by-messages` 把 `max_results` 改成「訊息數」上限（累計到達即停）——討論串會拆成
  多則訊息，想精準控制測試規模（例如「處理 10 則訊息」）時用這個，避免因為抓到一個
  10 幾則的大討論串而一次處理超出預期的量
- `--no-move` 只做 EML+RAG、不移動（測試用，且只處理第一封後停）
- `--headful` 顯示瀏覽器視窗（除錯用；瀏覽器一律用 `locale="en-US"` 開，避免 Verse
  跟著系統語系顯示中文介面、對不上寫死的英文 selector）

**一次性測試用環境變數**（例如某人已經自己分類好、想直接測某個資料夾+套用某個 proj
tag，不動預設的 04Done 流程）：
- `VERSE_SOURCE_FOLDER`：來源資料夾，預設 `04Done`。支援 `>` 分隔的巢狀路徑
  （例如 `"工程專案>JSR量產建置"`），會依序展開每一層父資料夾再點擊最後一層
- `VERSE_TARGET_FOLDER`：目標資料夾，預設 `domdom`
- `VERSE_PROJ_TAG`：設定後，Hindsight `tags` 會多加一個 `proj:{值}`（例如
  `tags=["mail", "proj:四廠JSR_B棟HVM產線建置"]`），沒設就跟預設一樣只有 `["mail"]`
  （proj 分類本身仍是暫緩，這只是先接受已知的手動分類結果，不是重新啟用自動判斷）

建議用 Bash `run_in_background: true` 執行，搭配 Monitor 監看
（成功標記：`結果已寫入`；失敗標記：`✗` / `Traceback`）。

**接著自動上傳 Gmail**（正式歸檔後一定要執行，`--no-move` 試跑則略過）：

```bash
python ~/.claude/skills/hcl-verse-RAG/verse_upload_gmail.py
```

把 `EML_OUTPUT_DIR`（部門共用網路磁碟，見下方）的 EML（每則訊息各自一個檔案，
檔名就是 `{unid}.eml`）批次 import 到 Gmail 標籤 `Notes_Import_v2`（跟舊格式的
`Notes_Import` 標籤區隔開來，方便分辨這批是訊息級拆分+UNID 命名之後的新格式），
成功後搬到
`~/Documents/eml to gamil/eml_done/`。沿用既有 OAuth 憑證/token
（`~/Documents/eml to gamil/`），有自己的 log 去重，重跑只補上次失敗的。每則 EML
只帶自己的 `Message-ID`，不組 `In-Reply-To`（3.14.0 拿掉，見上方說明），Gmail 裡
每則訊息各自獨立，不會自動合併討論串。

**代簽別人帳號測試**（例如同事已經分類好信件、想用他的帳號跑）：
- Verse 登入：直接沿用 `/hcl-notes-approval` 的帳密檔（`~/.hermes/.env.{name}`），
  執行前手動 `source` 該檔案匯出 `HCL_USERNAME`/`HCL_PASSWORD`（本腳本不像
  `/hcl-notes-approval` 支援 `HCL_ENV_FILE` 切換，因為讀檔邏輯用 `setdefault()`，
  只要這兩個變數執行前已經在環境變數裡，就不會被預設的 `~/.hermes/.env` 覆蓋）
- Gmail 上傳：`GMAIL_OAUTH_DIR` 指向另一組獨立的 `credentials.json`/`token.json`
  目錄（預設 `~/Documents/eml to gamil`），避免覆蓋自己的 token。同一個
  `credentials.json`（OAuth client）可以共用——不同 Google 帳號各自走一次
  consent flow，各自拿到自己的 `token.json`，不需要每個人都申請新的 OAuth client。
  第一次要對方本人在這台機器上完成 Google 登入+同意畫面（`run_local_server`
  跳出瀏覽器，只能本人操作，不能代為輸入帳密/代點同意）

## 流程（每一列信件）

1. 登入 Verse（`locale="en-US"`）→ `open_folder("04Done")` 進指定資料夾
2. 取清單**最上面那封**點開，同時攔截每則訊息展開時打出的 `OpenDocument` 網路請求，
   取得每則訊息在 Domino 資料庫裡的真實 **UNID**（`open_row_and_get_block_unids()`）
3. 抓整串（thread 級）header/raw——這份只作為資料夾層級摘要跟「一則訊息都抓不到」時的
   保底 fallback，**不是 EML 的主要來源**（EML 已改成逐則，見步驟 4/6）
4. **逐則訊息**（`extract_message_block()`，跟 Verse 的 accordion 展開順序一一對應）：
   - 沒有完整表頭的（Verse 自己判定內容已被後面訊息的引用完整涵蓋、只給精簡摘要）→ 跳過
   - `clean_body()`：剝 UI chrome 雜訊 → `quote_stripper.strip_quoted_history()` 砍掉
     引用歷史（寄件人:/发件人:/寄件者:/From:+Sent:/-----Original Message-----/
     -----郵件原件-----/Notes 內嵌 `"名字" ---日期---` 等樣式，抓最早出現的位置砍）
     → 產生分支 A 用的 `body`（清完版）
   - `_strip_ui_noise()`（只剝 UI，不砍引用）→ 產生分支 B 用的 `eml_body`（保留完整引用歷史）
   - `resolve_sender()`：用 `email_mapping.py` 查公司通訊錄，把「me」換成目前登入帳號的
     姓名/email（不寫死特定帳號）；查不到的（外部聯絡人/離職同仁）記進未知聯絡人追蹤
   - to/cc 也分兩份：`resolve_recipients()` 解析成純姓名（分支 A 用，可讀性優先，
     不需要真的 email）／`substitute_me()` 保留原始 email（`to_raw`/`cc_raw`，分支 B 用，
     Gmail 匯入需要真實地址）
   - `document_id` = 這則訊息的 Domino UNID（抓不到才退回 `hash(sender|subject|date)` 備援）
5. 每則訊息各自：
   - **分支 A — ① RAG**：本地 `jina-embed`（llama-cpp-server，OpenAI-compatible API，
     不需要 OpenAI key）→ upsert 到 Qdrant collection `verse_emails`
     （payload 含 `subject`/`body`(清完版)/`from_email`/`from_name`/`to`/`cc`(姓名)/
     `date`/`sent_date`/`unid`/`attachments`
     （`[{name, path}, ...]`，`path` 指向另存在 `ATTACHMENTS_DIR` 的實體檔案，
     檔名前綴 unid 避免同名衝突——跟內嵌在 `.eml` 裡的附件是同一份資料另存一份，
     不是重新下載）
   - **分支 A — ② Hindsight retain**：`retain` 到 `EID` bank（明確帶 `bank_id="EID"`），
     `document_id`=UNID，`tags=["mail"]`（有設 `VERSE_PROJ_TAG` 時多一個
     `proj:{值}`）——**不是預設的 proj 分類**，用途是讓 `reflect()`/`recall()` 可以用
     `tags=["mail"]` 過濾，避免跟同一個 `EID` bank 裡其他 skill 寫入的資料——例如
     `hcl-notes-approval` 的簽核記錄——混在一起污染查詢結果），`content`/`metadata.to`
     都用姓名不用 email（`metadata` 不含 `cc`，已移除）
   - **分支 B — ③ EML 匯出**：**每則訊息各自一個 `.eml`**（不是整串一個），內文用
     `eml_body`（保留完整引用歷史，不截斷）→ 下載該則自己的附件（比對 UNID，
     `verify=False`）→ `pack_eml()` 帶 `Message-ID`=`make_message_id(unid)`（不組
     `In-Reply-To`/`References`，見檔案開頭說明）→ 存到 `EML_OUTPUT_DIR/{unid}.eml`
     ——**檔名就是 UNID**，不再用主旨/序號/寄件者組（那樣組出來的檔名不好查詢，也沒有
     實質用途；UNID 本身就是唯一 key，可以直接回頭比對 Qdrant/Hindsight 裡的同一筆資料）
6. **④ 移動**：按「Move to folder」→ 輸入目標資料夾名稱 → 該信移出來源資料夾。有些信件
   （實測案例：`[Confidential/秘密]` 機密信）Verse 本身就會停用這個動作，不是自動化的
   bug——這種信會記進 `skip_row_sigs`（見下方安全閥），略過繼續處理下一封，不會卡住整批
7. 那封消失，回到步驟 2 處理下一封最上面的，直到清空或達上限
8. 全部歸檔後：
   - 有新的/更新的未知聯絡人 → 產生/合併 `~/verse-export/external_contacts.xlsx`
     → 發 Google Chat 通知（見「未知聯絡人確認機制」章節）
   - **⑤ 上傳 Gmail**：`verse_upload_gmail.py` 批次 import（每則各自一封）→ 標籤
     `Notes_Import_v2` → 搬到 eml_done

**安全閥**（兩層，互相獨立）：
- **列層級去重**：記已處理列的簽章（`hash(subject|sender|snippet)`）；若最上面那列跟
  上一輪一樣，代表選列邏輯出問題（理論上不該發生，因為下面這條已經會排除移不動的信），
  立即停止，避免無限迴圈或重複索引
- **移動失敗略過名單**（`skip_row_sigs`）：選第一封信時，不盲用畫面最上面那列，而是
  依序找「第一封不在略過名單裡的信」。移動失敗時記進這個名單、`continue` 處理下一封，
  不整批停止——這是實測發現真的需要的：機密信 Verse 本身就停用移動，重試也不會好，
  而且這種活躍討論串常常因為有新回覆而一直排在最上面，不略過會永久卡住整批進度

## proj 分類（暫緩）

歸檔階段先不分類 `proj:`，`tags` 目前只有 `["mail"]`（來源標籤，見上方章節，跟
proj 分類是兩件事）。之後要補分類時：`document_id` 是可重算的固定值（UNID）、
Hindsight `retain` 是 upsert（同 id 直接覆蓋，重新呼叫要帶著同一個 `tags=["mail"]`
一起送，不然會被覆蓋掉）、`subject`/`body` 已經留在 Qdrant payload 裡 → 寫一支
backfill 腳本可以直接從 Qdrant 撈，不用重新爬 Verse，兩件事完全解耦。屆時 proj
分類可以用 `tags` 再加一個 `proj:xxx`（`tags=["mail", "proj:xxx"]`）或改用
`metadata` 存——兩種都可行，實際做的時候再定。`project_keywords.py` 的
`match_projects()`（多標籤加權）/`match_project()`（單一，向後相容）都還在，
只是目前沒被主流程呼叫。

## 身份解析（email ↔ 姓名）

`email_mapping.py` 查 PostgreSQL 公司通訊錄（`email_mapping` 表，`email` 欄位有 UNIQUE 鍵）：

- `resolve_me(HCL_USERNAME)` → 目前登入帳號的 `(email, 姓名)`，取代寫死特定帳號，
  多帳號（tzuyu/ycmu）代簽時也會抓對
- `email_to_name(email)` / `name_to_email(name)`：雙向查詢
- 主流程的 `resolve_sender(raw)` 回傳 `(email, name, found_in_directory)` 三元組；
  `substitute_me(raw)` 把 to/cc 字串裡的獨立「me」換成目前帳號的 email
- `resolve_recipients(raw)`：把 to/cc 字串裡每個 `"Name <email>"` 或純 email 都換成
  通訊錄查到的姓名，**只給分支 A（RAG/Hindsight）用**——分支 B（EML）要保留原始
  `to_raw`/`cc_raw`（含真實 email），不能走這個函式，因為 Gmail 匯入需要有效地址
- `_split_recipient_entries(raw)`：`resolve_recipients()`/`quote_recipient_header()`
  共用的收件人切分邏輯，不是單純逗號切——會辨識「Hsieh, Tata」這種西式「姓, 名」
  格式（單一英文姓氏 + 下一段緊接 `<email>`）合併回同一人，避免被逗號誤拆成兩人
- `quote_recipient_header(raw)`：組 `to_raw`/`cc_raw`（分支 B/EML 用）時，把顯示名稱
  含逗號的收件人加上雙引號（RFC 5322 合法格式），否則 Gmail 解析 `To:`/`Cc:` 信頭
  會把逗號當成收件人分隔符，一樣把「Hsieh, Tata」拆成兩個人

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
4. **讀回確認 → 回填舊資料**：已實作，獨立成 `hcl-verse-contacts-update` skill
   （不是這支腳本的一部分），觸發語是「hindsight聯絡人更新」。重點設計（詳見該
   skill 自己的 SKILL.md）：
   - Upsert 進**同一張** `email_mapping` 表（已確認公司通訊錄同步機制是 upsert，
     不會清掉手動加的列）
   - 用 email 全表掃描 Qdrant payload 的 `from_email`/`to`/`cc` 找出所有相關 UNID
   - **重新 `retain()` 之前，一定要先 `get_document(document_id=unid, bank_id="EID")`
     把舊的 `tags`/`document_metadata` 讀出來**，合併新姓名後整包一起送出——
     `retain()` 是整段覆蓋不是 merge，只帶更新過的內容重新 retain，沒帶到的
     `tags`/`metadata` 欄位會直接消失
   - Qdrant 端用 `set_payload()` 同步更新 `from_name`
   - Qdrant 查無資料是正常情況（代表當初 RAG/Hindsight 那步可能失敗過），不是錯誤，
     跳過回填即可

## 結果呈現

讀取兩個結果檔（路徑都是 `tempfile.gettempdir()` 算出來的，WSL/Linux 下是
`/tmp/...`，原生 Windows Python 下會是別的路徑如 `%TEMP%\...`，不要寫死 `/tmp`）：
- 歸檔：`verse_archive_pipeline_result.json`
  `{source, target, no_move, archived_date, sent_date_range, processed, message_total, rag_ok, hindsight_ok, moved, emails[]}`
  （`emails[]` 每筆現在含 `message_count`/`rag_ok`/`hindsight_ok`，因為一列信件可能拆成多則訊息）
- 上傳：`verse_upload_gmail_result.json`
  `{total, uploaded, failed, label, done_folder, results[]}`

呈現格式：

```
✓ 從 04Done 歸檔 N 封（共 M 則訊息）→ domdom
  RAG 索引：M 成功 / Hindsight：M 成功
  EML：EML_OUTPUT_DIR（部門共用網路磁碟，每則訊息各自一個 {unid}.eml，含附件，保留完整引用歷史）
  Gmail：上傳 M 封到 Notes_Import_v2（搬到 eml_done）
  外部聯絡人待確認：X 位（已發 Google Chat 通知 / 略過）
  [1] [5/29] PharmaSuite 專案週報（3 則訊息, RAG 3/3, Hindsight 3/3, 3 附件, moved, gmail ✓）
  [2] ...
✗ 失敗：列出 rag/eml/hindsight/move/gmail 任一失敗的信件主旨
```

> 注意：`rec["eml"]` 現在是**每列信件的 `.eml` 路徑清單**（一個討論串可能對應多個檔案），
> 不再是單一路徑；上傳 Gmail 的封數是 `message_total`（訊息數），不是 `processed`（信件數）。

## 寫入 Hindsight / Qdrant（分支 A）

每則訊息在歸檔時自動 retain + upsert（不需手動補寫）。關鍵欄位：

- `document_id` = 該則訊息的 Domino UNID（idempotent，重跑/重複歸檔同一封不會重複）
- `timestamp` = `sent_date`（信件真實寄件日，非歸檔日）
- `content` = 清乾淨且砍過引用歷史的內文（`body`），寄件者用姓名（`resolve_sender` 解析），
  不預摘要，讓 Hindsight 自行抽取 facts
- `tags` = `["mail"]`（有設 `VERSE_PROJ_TAG` 時多一個 `proj:{值}`，見「執行」章節，
  預設仍是暫緩的 proj 分類），用途是讓 `reflect()`/`recall()` 可以帶 `tags=["mail"]`
  過濾，只搜尋 Verse 信件這個來源的記憶，避免跟同一個 `EID` bank 裡其他 skill 寫入的
  資料（例如 `hcl-notes-approval` 的簽核記錄）混在一起——實測發現不過濾的話，
  `reflect()` 會把不相關的簽核記錄也撈進來當雜訊
- `metadata` = `{subject, from_email, from_name, to, unid, sent_date}`（**不含 `cc`**，
  已移除）—— `to` 是**姓名**（`resolve_recipients()` 解析）。**不含 `thread_id`/
  `reply_to_unid`**（3.14.0 拿掉，見檔案開頭說明）
- `bank_id` = `EID`（`verse_archive_pipeline.py`/`gmail_backfill.py` 都在 `retain()`
  簽名明確帶 `bank_id="EID"` 預設值，不再依賴 Hindsight server 端的隱式預設）
- Qdrant payload 額外多一個 `attachments` 欄位（`[{name, path}, ...]`），`path` 指向
  `ATTACHMENTS_DIR`（`EML_OUTPUT_DIR/attachments/`，部門共用網路磁碟）裡另存的實體
  檔案（檔名前綴 unid）

> **已知未清理的殘留**：`EID` bank 在 3.10.0 建過一個 directive，內容說明
> `metadata.thread_id`/`reply_to_unid` 的語意，現在這兩個欄位已經不寫了，這個
> directive 變成過時/可能誤導 `reflect()` 推論——還沒有動手清掉，之後有需要再處理
> （`list_directives()`/刪除 directive 的 API 待查）。
> 分支 B（EML/Gmail）用的是同一批訊息的 `eml_body`/`to_raw`/`cc_raw`，跟這裡的
> `body`/`to` 是分開的兩份資料，互不影響——見上面「流程」章節。Qdrant payload
> 仍然保留 `cc`（只有 Hindsight metadata 拿掉），兩邊沒有綁在一起。

## 會議記錄 / 報價單附件 -> RAGAnything（`meeting_quote_upload.py`）

信件附件檔名或主旨符合關鍵字（`MEETING_KEYWORDS`/`QUOTE_KEYWORDS`）的 `.pdf`，
會另外送進共用知識庫 RAGAnything（跟這個 pipeline 的 Qdrant/Hindsight 是不同系統，
設定見 `C:\Users\EID\Documents\Claude\ShuHsing\WSL\CLAUDE.md`）。**分兩階段**，不在
歸檔當下同步解析（3.15.0 改版，原因：單一附件解析可能要跑好幾分鐘，同步做會拖慢
整支歸檔 pipeline）：

1. **歸檔當下**（`process_meeting_quote_attachments()`）：只把符合關鍵字的 `.pdf`
   附件另存一份到 `MEETING_QUOTE_STAGING_DIR`（部門共用網路磁碟，預設
   `\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\meeting minutes`，可用同名
   環境變數覆寫），旁邊多存一個同名 `.json` sidecar 記 `unid`/`subject`/
   `sender_name`/`sent_date`/`labels`（RAGAnything 只認檔案本身，不會保留這些
   metadata，一定要另外存，事後批次處理要用）。不呼叫 RAGAnything，不寫 Hindsight，
   幾乎不花時間
2. **歸檔全部跑完後另外執行**：
   ```bash
   python ~/.claude/skills/hcl-verse-RAG/meeting_quote_batch_process.py
   ```
   掃描 `MEETING_QUOTE_STAGING_DIR` 頂層的 `.json` sidecar，逐一讀回 metadata →
   `save_to_inputs()` 複製進 RAGAnything 的 WSL inputs 目錄 → `docker compose exec`
   跑 `process_pdf.py`（`PROCESS_TIMEOUT_SEC=1800`，容錯到 30 分鐘）→ 「會議記錄」類
   額外去 output 目錄撈解析出的 markdown 全文，寫進 Hindsight（`EID` bank，
   `tags=["mail", "meeting-minutes"]`，`document_id`=`{unid}_meeting_{檔名hash}`，
   不會跟訊息本體的 `document_id`=UNID 撞到）。「報價單」類只送 RAGAnything，不寫
   Hindsight。成功的（RAGAnything 成功，且「會議記錄」類 Hindsight 也要成功）搬到
   `MEETING_QUOTE_STAGING_DIR/done/`，失敗的留在原地，重跑只補失敗的

只處理 `.pdf`——RAGAnything/MinerU 理論上能解析 docx/pptx，但目前只在 PDF 上實測過，
其他格式先跳過。RAGAnything 知識庫**不分 project/workspace**，機密內容也會丟進去
（已跟使用者確認過可以接受）。

## 技術細節（除錯參考）

- 信件清單 selector：`.seq-msg-row`（列文字含 `From / Subject / Message abstract`；討論串多一行 `Count\nN`）
- 閱讀窗格：`.preview-container`；單則訊息容器：`.preview-container .pim-mailread-container[aria-expanded]`
  （比單獨用 `.preview-container [aria-expanded]` 精準，避免把摺疊摘要跟完整訊息都算進去、造成重複計數）
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
- **`reply_to_unid` 已拿掉**（3.14.0）：曾經有一版用 `strip_quoted_history_with_identity()`
  抽出「被引用的是誰/什麼時候」、跟同一批訊息比對配對，但這個機制只在「同一次展開的
  這批訊息」範圍內有效——每天執行的話，同一討論串較早的訊息通常前幾天就歸檔移出
  Verse 了，硬要配對只會得到不完整關聯，所以整個拿掉，改回單純的 `clean_body()`/
  `strip_quoted_history()`（不抽身份）
- **EML 內容來源不是同一份**：分支 A 的 `body`（RAG/Hindsight 用）已經被
  `strip_quoted_history()` 砍過引用歷史；分支 B 的 `eml_body`（EML/Gmail
  用）只走 `_strip_ui_noise()`，**保留完整引用歷史**，兩者共用同一段抓下來的原始
  `blk["body"]`，只是後續加工方式不同——改動任一邊時要注意別把這兩條路徑接錯
- 移動鈕：`button.action.pim-move-to-folder.icon`（取**可見**的那個）。原本以為
  資料夾檢視一定是 `action-bar collapse-stage-0`、不會有 `action-tray-populated`
  （那是 Inbox 檢視才有）——**這個假設對訊息數少的討論串成立，但對很大的討論串
  （實測案例：46 則訊息）不成立**，Verse 會切到跟 Inbox 一樣的 `action-tray-populated`
  工具列。已知有些信件（實測案例：`[Confidential/秘密]` 機密信）這個按鈕在該工具列
  裡確實存在但 `visible=False`，等多久都不會變 `True`，翻遍「More actions」選單也沒有
  替代選項——這是 Verse 本身針對機密信停用移動的限制，不是自動化的 bug，遇到就該
  略過（見「流程」章節的 `skip_row_sigs` 安全閥），重試沒有用
- 展開巢狀父資料夾（`_expand_treeitem_by_name()`，給 `VERSE_SOURCE_FOLDER` 的
  `>` 巢狀路徑用）：**一定要用 Playwright 對 `.folder-icon` 子元素做真的滑鼠
  click()**，實測過對 `<li>` 本身用 `page.evaluate()` 發 JS 合成 `.click()`
  完全不會觸發這個 Dojo widget 的展開行為（DOM 看起來點了但畫面沒展開）
- 移動 popup：`div.folder-tray-float.show`，輸入 `input.folder-search-input` 後選
  `[role='treeitem']:visible:has-text(目標資料夾名稱)`（精準比對，避免選錯同名項目）。
  **搜尋框對含括號的資料夾名稱完全比對不到**（實測：打完整的
  `已上傳Gmail(暫時找信)` 回傳 0 筆，去掉結尾括號註記、只打 `已上傳Gmail` 才篩得到）
  ——輸入搜尋字串前先用 regex 去掉結尾的 `(...)`/`（...）`，但實際點擊比對仍用完整
  名稱，確保選到的是名稱完全相符的那個
- 附件連結：`$File/{UNID}/...?OpenElement`（Domino 標準 URL，網址本身帶該則訊息的
  UNID）；下載需 `verify=False`（公司內部憑證）。EML 改成逐則後，附件也要照
  `UNID in href` 比對分給對應那則的 `.eml`，不能整批塞給第一則
- 附件命名：優先信任 URL 的 `FileName=` 參數（Domino 標準做法，可靠），`a.innerText`
  只當備援。原本優先信任 `innerText`、只在空字串或裸副檔名時才退回 URL，但 Verse
  常把 `innerText` 渲染成籠統的操作文字（例如「Download file」），不是空字串也不算
  短副檔名，舊判斷抓不到這種情況，導致附件檔名整個顯示成「Download file」
- **`EML_OUTPUT_DIR`**：分支 B 的 `.eml`/附件實際存放位置，預設是部門共用網路磁碟
  `\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\eml`（可用 `EML_OUTPUT_DIR`
  環境變數覆寫）；`ATTACHMENTS_DIR` = `{EML_OUTPUT_DIR}\attachments`，跟著移到同一個
  網路資料夾底下，不再留在本機 `~/verse-export`。這個路徑需要 SMB 存取權限——
  第一次從自動化環境（非使用者互動桌面 session）連線時，`Test-Path`/`os.makedirs`
  可能因為 SMB session 還沒建立而回報連不上，跑一次 `net use "\\10.11.1.40\..."`
  觸發連線後就正常了，跟帳號密碼、分享權限設定都無關，純粹是 session 時機問題
- 日期：`.pim-mailread-sentdate` 底下有 `.pimMailShort`（縮寫）跟 `.pimMailLong`（完整
  時間戳）兩個 span，兩者都在 innerText 裡（不是只有畫面顯示的縮寫），取最長那行即可拿到
  完整時間 → `normalize_sent_date()` 正規化成 ISO；缺年份時推算（月份比今天超前 >7 天 → 去年）。
  避開 `[class*="ate"]`（會混進行事曆 widget 雜訊）
- Embedding：長討論串可能超過 8192 token 上限 → `get_embedding()` 用 tiktoken 截斷到 8000 token
- Qdrant：`http://10.11.1.40:6333`（跑在 Synology NAS 上，**常駐服務**，不是 WSL
  docker、不需要每次手動啟動），collection `verse_emails`，向量 **2048** 維（實測
  jina-embeddings-v4 實際輸出 2048 維，不是原本假設的 1024——曾經因為維度不合導致
  100% upsert 失敗，`.../v1/embeddings` 帶 `dimensions` 參數截斷也不會生效，這個
  llama-cpp-server 版本不支援 Matryoshka 截斷，四支腳本的 `VECTOR_SIZE` 都要維持 2048）
- **Qdrant 長連線會卡死**（3.14.0 修正，實測重現 3 次）：`QdrantClient` 是模組層級
  單一長壽命實例、走 HTTP keep-alive，跑一段時間（批次跑到一半、中間有其他 I/O
  空檔）之後，NAS 那端會把閒置的 keep-alive 連線悄悄關掉（TCP 狀態變 `CloseWait`），
  但下一次呼叫還是想重用這條殭屍連線，卡住不會拋例外。試過 `QdrantClient(timeout=30)`
  跟 process 全域 `socket.setdefaulttimeout(60)` 兩層防護都沒用（等超過設定時間還是沒
  反應，研判 timeout 設定沒有正確套用到這個底層連線重用的路徑）。**真正有效的修法**：
  `already_indexed()`/`upsert()` 改成每次呼叫都用 `_fresh_qdrant()` 開一支全新的
  `QdrantClient`（不重用連線池），等同於手動 curl 每次都是新連線、每次都秒回的效果。
  代價是每次呼叫多一次 TCP handshake，但在同一台區網內可忽略不計。四支查詢腳本
  （`gmail_backfill.py`/`verse_query.py`/`verse_rag_search.py`/
  `hcl-verse-contacts-update/update_external_contacts.py`）目前只加了
  `timeout=30`（治標），沒有跟著改成 fresh-client-per-call（治本）——這些腳本呼叫
  頻率低很多，暫時沒有實際卡住過，但架構上跟 `verse_archive_pipeline.py` 一樣有
  同樣的風險，之後有需要再一併修
- Embedding 伺服器：本地 llama-cpp-server（CPU only，`-ngl 0`，RAM 約 5.9GB），跑在
  WSL，用 **systemd on-demand socket 架構**管理，四支腳本要接的是
  `http://localhost:8081/v1`：
  - `jina-embed.socket`（`ListenStream=0.0.0.0:8081`，`Accept=yes`）：對外的穩定
    入口，每個新連線交給 `jina-embed@.service`（`proxy-relay.py`）處理
  - `proxy-relay.py`：收到連線先檢查 backend（127.0.0.1:8090）健不健康，沒在跑就
    `systemctl start jina-embed.service` 喚醒，等 healthy 後再把連線原封不動轉發過去
  - `jina-embed.service`：實際跑模型的 llama-server，監聽 **127.0.0.1:8090**——這是
    背後 backend 專用 port，**不要讓 pipeline 直接接這個**，因為
    `jina-embed-idle.timer`（`idle-watchdog.sh`）會在閒置 ~10 分鐘後把它關掉省
    RAM，長時間跑 pipeline 中途接不到會斷線；一定要接 8081 讓 proxy 需要時自動喚醒
  - 模型：`jina-embed`（實際對應 `jina-embeddings-v4-text-retrieval-Q4_K_M.gguf`）
  `OPENAI_KEY` 只在傳給 `OpenAI()` client 建構子時當佔位字串用（本地伺服器不驗證），
  可用 `EMBEDDING_API_BASE`/`EMBEDDING_MODEL` 環境變數覆寫
- PostgreSQL（`email_mapping` 表 + 之後的 `update_external_contacts.py` 用）：
  host/port/db/user/password 存在 `~/.hermes/.env` 的 `PG_*` 變數
- 仍想語意搜尋已索引的信：`python ~/.claude/skills/hcl-verse-RAG/verse_rag_search.py "查詢" [top_k]`（保留在磁碟）
- Gmail 上傳：`verse_upload_gmail.py [eml_folder] [--label] [--done] [--log]`，
  `eml_folder` 預設值改成 `EML_OUTPUT_DIR`（跟 `verse_archive_pipeline.py` 用同一組
  預設值 + 同名環境變數，兩邊不用互相 import 也能保持一致）。用 Gmail
  `messages.import_`(`neverMarkSpam`)。OAuth 憑證/token 在 `~/Documents/eml to gamil/`
  （這台機器一開始完全沒有這個資料夾，`credentials.json`/`token.json` 是後來從別的
  地方拿過來放的，`token.json` 內含 `refresh_token`，不用重新跑一次瀏覽器同意畫面），
  token 過期會自動 refresh（非互動）；`fix_eml_content` 修補缺/重複的 From 欄位；
  log 去重（`verse_upload_log.txt`），重跑只補失敗的

## 查詢已歸檔的信件

使用 `verse_query.py`（另一支腳本）：

```bash
python ~/.claude/skills/hcl-verse-RAG/verse_query.py --search "帆宣請款"        # 找信
python ~/.claude/skills/hcl-verse-RAG/verse_query.py --reflect "V3F 最新狀態"   # 問答
python ~/.claude/skills/hcl-verse-RAG/verse_query.py --model "PharmaSuite/MES"  # 進度摘要
```

## Gmail Backfill（一次性）

將整個 Gmail 信箱 backfill 到 Hindsight + Qdrant：

```bash
python ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py             # 全部
python ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py --max 100   # 前 100 封測試
python ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py --dry-run   # 只印不寫入
python ~/.claude/skills/hcl-verse-RAG/gmail_backfill.py --reset     # 清進度從頭來
```

- `document_id` = `hash(from|subject|date)`，idempotent，可重跑/斷點續跑
- 進度記錄在 `~/.claude/skills/hcl-verse-RAG/backfill_progress.json`
- OAuth 憑證/token 在 `~/Documents/eml to gamil/`

> 這支是獨立的一次性 Gmail 信箱 backfill，跟 04Done pipeline 的訊息級 UNID 機制無關，
> 沒有跟著這次升級（仍是整封信一個 document_id）。

## 已知缺口 / 待辦

- **🚨 最優先待處理（2026-07-13）：所有帳號的 Verse 都已經關掉討論串分組**
  （shuhsing／tzuyu／ycmu 三個目前有帳密檔的帳號都做了，Inbox 都驗證過生效），
  這支 pipeline 目前完全沒有針對這個新畫面驗證過，繼續跑歸檔前一定要先確認：
  - **怎麼關的**：Verse 工具列有個按鈕 `[class*='toggle-threads']`（title="Don't
    group email conversations as a thread"），已經在 shuhsing/tzuyu 的 Inbox
    跟 ycmu 的「工程專案 > JSR量產建置」資料夾點下去並確認生效——點擊前資料夾清單
    每一列可能是多則訊息合併的討論串（帶 `Count\nN`），點擊後**每一列變成單一則
    訊息**，不再合併、不再有 `Count`（例如原本合併成一列的「Python安裝」討論串，
    現在拆成「回覆: Python安裝」好幾列各自獨立）
  - **這是帳號層級設定**，會影響該帳號所有資料夾（含以後新增的資料夾），三個帳號
    都是在單一資料夾/Inbox 點擊後驗證，沒有逐一確認過該帳號底下每個資料夾都生效
    （合理假設整個帳號都套用了，但沒有窮舉驗證）。**如果之後有新帳號要用這支
    pipeline，記得也要先手動點這個按鈕**，不然這個帳號會踩到下面這個相容性問題
  - **為什麼這件事很重要**：現在整支 `verse_archive_pipeline.py` 的訊息級抓取邏輯
    （`open_row_and_get_block_unids()`/`extract_message_block()`）完全是照著
    「一列可能是多則訊息、要展開手風琴逐一取」這個前提寫的（見「流程」章節步驟
    2/4）。討論串分組關掉後，一列直接就是一則訊息本身，沒有手風琴可展開——**這套
    邏輯在新畫面下完全沒測過，可能整個抓不到東西，也可能剛好還能動（例如
    `n_blocks` 變成固定 1、`extract_message_block(page, 0)` 剛好還抓得到那唯一
    一則的內容），沒驗證過不能假設**
  - **下一步**：在正式跑任何批次歸檔之前（不分哪個帳號，三個都已經受影響），先用
    `--no-move` 對任一資料夾跑一次，仔細核對印出來的
    `message_count`/`RAG`/`Hindsight` 數字合不合理（例如原本一個討論串該有 7 則，
    關掉分組後如果邏輯沒跟上，可能會誤判成 1 則或完全抓空）。如果邏輯真的需要
    因應新畫面調整，這是一個不小的改動，需要重新設計「怎麼判斷這一列有幾則訊息」
    這件事——不能想當然爾套用舊邏輯
- proj 分類 backfill 腳本還沒寫（暫緩中，等實際批次 review 時再做）
- Embedding server（`jina-embed.socket`，port 8081）背後的 backend 閒置 ~10 分鐘會被
  `jina-embed-idle.timer` 自動關掉省 RAM——正常情況下接 8081 會自動喚醒，但喚醒
  過程（`systemctl start` + 等 healthy）大約要幾秒到十幾秒，長討論串批次歸檔的
  第一個 embedding 請求可能會等比較久，屬於預期行為不是 bug
- `EID` bank 裡 3.10.0 建立的 Hindsight directive（說明 `thread_id`/`reply_to_unid`
  語意）已經過時（3.14.0 拿掉這兩個欄位），還沒清掉，可能誤導 `reflect()` 推論
- `gmail_backfill.py`/`verse_query.py`/`verse_rag_search.py`/
  `update_external_contacts.py` 的 `QdrantClient` 只加了 `timeout=30`，沒有跟著
  `verse_archive_pipeline.py` 一起改成 fresh-client-per-call（治本）——這幾支腳本
  呼叫頻率低，暫時沒有實際卡住過，但架構上有一樣的風險
- 「已上傳Gmail(暫時找信)」這種資料夾名稱裡的括號會讓 Verse 搜尋框搜尋不到（已在
  `move_to_folder()` 修過，見「技術細節」），但如果之後其他地方也要用資料夾名稱搜尋
  /比對，要記得這個限制
- `meeting_quote_batch_process.py`（3.15.0 新增）還沒實際跑過真的 RAGAnything 處理，
  只驗證過存檔/sidecar/`find_pending()` 這幾個環節，正式跑之前建議先用一份真實會議
  記錄 PDF 端對端測一次，確認 `done/` 搬移跟 Hindsight 全文寫入都正常

## Changelog

- 3.15.0 (2026-07-13): 會議記錄/報價單附件改成兩階段處理，不再拖慢歸檔本身
  - **背景**：`meeting_quote_upload.py`（3.14.0 才發現這支檔案存在，先前一直沒寫進
    SKILL.md）原本在歸檔迴圈裡同步呼叫 `upload_to_raganything()`（`docker compose
    exec` 跑 MinerU 版面解析 + LLM 圖表說明），`PROCESS_TIMEOUT_SEC=1800` 給到 30
    分鐘容錯——代表處理到一封帶會議記錄/報價單附件的信，整支歸檔 pipeline 會卡在
    那邊等，其他信件都得排隊
  - **改成兩階段**：歸檔當下（`process_meeting_quote_attachments()`）只把符合關鍵字
    的 `.pdf` 另存到新的 `MEETING_QUOTE_STAGING_DIR`（部門共用網路磁碟，預設
    `\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\meeting minutes`），旁邊多存
    一個同名 `.json` sidecar 記 `unid`/`subject`/`sender_name`/`sent_date`/`labels`
    （RAGAnything 只認檔案本身，metadata 一定要另外存）。不再呼叫 RAGAnything，不寫
    Hindsight，幾乎不花時間，不拖慢歸檔
  - **新增 `meeting_quote_batch_process.py`**：歸檔全部跑完後另外執行，掃描
    staging 目錄的 sidecar、逐一送進 RAGAnything、「會議記錄」類額外把全文寫進
    Hindsight，成功的搬到 `done/` 子目錄（沿用 EML 上傳 Gmail 的 done 慣例），失敗的
    留在原地方便重跑
  - **範圍**：使用者確認會議記錄跟報價單附件都要走這個兩階段設計（同根源問題：
    RAGAnything 同步呼叫拖慢歸檔），metadata 傳遞方式選用 JSON sidecar（不是編碼進
    檔名），理由是批次處理時不用回頭查 Verse/Qdrant，也不會因為主旨太長讓檔名難處理
  - `process_meeting_quote_attachments()` 簽名拿掉 `hindsight` 參數（歸檔當下不再需要，
    改由 `meeting_quote_batch_process.py` 自己建立 `HindsightClient`）
  - 已用假資料端對端驗證存檔+sidecar 寫入、`meeting_quote_batch_process.py` 的
    `find_pending()` 能正確配對 `.pdf`+`.json` 並讀回 metadata（測試資料已清除，
    沒有留在共用資料夾裡）；沒有實際跑過 RAGAnything 那一段（那部分邏輯沿用既有、
    先前已經驗證過的 `save_to_inputs()`/`upload_to_raganything()`/
    `find_parsed_markdown()`/`write_meeting_to_hindsight()`，只是換了呼叫時機）
- 3.14.0 (2026-07-13): 移除 thread_id/reply_to_unid + 修正批次歸檔卡死問題 + 新增
  一次性測試用環境變數。用彥池（ycmu）帳號、「工程專案 > JSR量產建置」資料夾實測驗證
  - **移除 `thread_id`/`reply_to_unid`**：這個 pipeline 是每天執行的，同一討論串較早的
    訊息通常前幾天就已經歸檔並移出 Verse，當下這一批根本看不到完整討論串，硬要配對
    只會得到不完整、誤導性的關聯。拿掉 `make_thread_id()`/`match_reply_to()`/
    `clean_body_and_identify()` 三個函式，`clean_body_and_identify()` 呼叫點改回單純的
    `clean_body()`。Qdrant payload、Hindsight metadata 都不再帶這兩個欄位；分支 B 的
    `pack_eml()` 只帶自己的 `Message-ID`，不再組 `In-Reply-To`/`References`，Gmail 裡
    每則訊息變成獨立的信，不自動合併討論串（這是使用者明確接受的 trade-off）。
    `meeting_quote_upload.py` 的 `thread_id` 參數一併拿掉。**已知殘留**：`EID` bank
    3.10.0 建立的 Hindsight directive 還在說明這兩個欄位的語意，現在已經過時，還沒
    清掉（見「寫入 Hindsight / Qdrant」章節）
  - **修正 Qdrant 長連線卡死**：`already_indexed()`/`upsert()` 改成 `_fresh_qdrant()`
    每次開新連線，不重用連線池——這個 bug 造成整支 pipeline 無限期卡住，實測重現
    3 次（背景執行到一半、CPU 完全不動、TCP 連線卡在 `CloseWait`），`timeout=30`
    跟全域 `socket.setdefaulttimeout(60)` 都沒能解決，只有不重用連線才真正有效。
    詳見「技術細節」章節的完整說明
  - **修正資料夾巢狀展開**：`open_folder()`/`_expand_treeitem_by_name()` 支援
    `VERSE_SOURCE_FOLDER` 用 `>` 表示的巢狀路徑（例如 `"工程專案>JSR量產建置"`），
    根因是原本用 `page.evaluate()` 發 JS 合成 click 不會觸發 Dojo widget 展開，
    改用 Playwright 對 `.folder-icon` 做真的滑鼠點擊才成功
  - **修正移動時資料夾名稱含括號搜尋不到**：`move_to_folder()` 打進搜尋框前先用
    regex 去掉結尾的 `(...)`/`（...）`（例如 `已上傳Gmail(暫時找信)` 搜尋不到，去掉
    括號打 `已上傳Gmail` 才篩得到），實際點擊比對仍用完整名稱
  - **新增移動失敗略過名單（`skip_row_sigs`）**：實測發現有些信件（`[Confidential/秘密]`
    機密信）Verse 本身就會停用移動這個動作（等 30 秒、翻遍 More actions 選單都確認過
    真的沒有這個選項，不是渲染延遲），重試也不會好。這種信之前會讓整支 pipeline 安全
    停止，現在改成記進略過名單、`continue` 處理下一封，不整批卡住；同時發現這種活躍
    討論串常因為有新回覆一直排在資料夾最上面，選列邏輯也從「盲用 `rows.first`」改成
    「依序找第一封不在略過名單裡的信」
  - **新增一次性測試用環境變數**：`VERSE_SOURCE_FOLDER`（來源資料夾，預設 `04Done`）、
    `VERSE_TARGET_FOLDER`（目標資料夾，預設 `domdom`）、`VERSE_PROJ_TAG`（設定後
    Hindsight tags 多一個 `proj:{值}`）——用於「某人已經自己分類好信件，想直接測某個
    資料夾+套用某個 proj tag」這種情境，不影響預設的 04Done 流程
  - **新增 `verse_upload_gmail.py` 的 `GMAIL_OAUTH_DIR`**：代簽別人帳號上傳 Gmail 時，
    指向另一組獨立的 `credentials.json`/`token.json` 目錄，避免覆蓋自己的 token。
    已驗證同一個 `credentials.json`（OAuth client）可以共用，不同 Google 帳號各自走
    一次 consent flow、各自拿到自己的 token，不需要每個人申請新的 OAuth client
  - **補上其餘 HTTP 呼叫的 timeout**：`HindsightClient.__init__()` 的 initialize
    請求、embedding client（`OpenAI(timeout=60)`）、附件下載
    （`session.get(..., timeout=120)`）原本都沒設 timeout，一併補上，避免類似
    Qdrant 那種卡死重演。`gmail_backfill.py`/`verse_query.py`/`verse_rag_search.py`/
    `update_external_contacts.py` 的 `QdrantClient` 也加上 `timeout=30`（只是治標，
    詳見「技術細節」章節說明為什麼這幾支腳本沒有跟著改成 fresh-client-per-call）
  - 端對端驗證：用彥池的 `ycmu` 帳號、「工程專案 > JSR量產建置」資料夾（已人工分類好
    的專案信件）實測跑通全流程——RAG/Hindsight 帶正確 proj tag、EML 上傳到彥池自己的
    Gmail（獨立的 `GMAIL_OAUTH_DIR`）、機密信正確略過不卡住整批
- 3.13.0 (2026-07-12): 新增 `--by-messages` 參數 + 補上 to/cc 收件人的未知聯絡人追蹤
  - **`--by-messages`**：`max_results` 原本只能當「信件/列數」上限，討論串一拆開
    可能遠超預期（例如「10 封」實測拆出 30 則訊息，其中一個討論串就佔了 17 則）。
    新增這個旗標把上限改成「訊息數」，累計到達即停（可能在討論串中途完整跑完
    當下那封信才停，不會精準卡在剛好那個數字，但已經比信件數可控很多）
  - **補上 to/cc 的未知聯絡人追蹤**：`resolve_recipients()` 之前只有
    `resolve_sender()`（寄件者）查不到 `email_mapping` 時會呼叫
    `track_unknown_contact()` 記錄進 `external_contacts_state.json`，to/cc 裡查不到
    的人完全沒有追蹤——這是 3.12.0 changelog 就記錄過的已知缺口（不對稱：寄件者有
    追蹤，收件人沒有）。這次補上：`resolve_recipients()` 新增 `contacts_state`/
    `date_str` 參數，帶了才會在「有 `<email>` 但查不到」的收件人身上呼叫
    `track_unknown_contact()`，機制跟 `resolve_sender()` 完全一致
  - **範圍限制（沒解決的部分）**：這次只補了「有 email 但查不到」的情況。實測
    真實案例（興忠行討論串，cc 40+ 人）發現大部分查不到的收件人根本沒有 email
    可查——Verse 顯示成 `'CIC/Jeff Ho'`、`'timl'` 這種純文字（外部公司自己的
    Domino canonical name 或純顯示名，格式跟我們能解析的 `Name/OU/Org` 不同，
    `resolve_canonical_names_via_api()` 也解不了外部網域的名字）。
    `track_unknown_contact()`/Excel/`email_mapping` upsert 全部以 `email` 當
    必填的唯一 key，沒有 email 就沒有東西可以追蹤、也沒有東西可以寫回
    `email_mapping`——原本評估過在 Excel 加一欄讓人工補填「真正的 email」、
    改 `hcl-verse-contacts-update` 的 `update_external_contacts.py` 讀這個新
    欄位（跨兩個 skill 的較大改動），實作後發現這樣做等於是自動用一個看起來
    合理但沒有實際驗證過的假設去猜這些人是誰，風險比想像中高，已經整個撤掉、
    回到只做前面「有 email 但查不到」這個較小範圍的修正。這種「查不到、又沒有
    email 可查」的情況之後改成人工回頭去 Verse 找原信確認身份，並用 email
    通知使用者，不再嘗試自動化這一段
  - 這次測試同時發現 `already_indexed()`/清除腳本用的 `id_to_uuid()` 曾經被誤植成
    MD5 雜湊版本（實際程式碼是把 UNID 補零/截斷成 32 碼直接插入 dash，不是雜湊），
    用錯公式清資料會刪到不存在的 id、看起來「成功」但其實沒刪到東西——這只是這次
    手動清測試資料時人為犯的錯，不是程式本身的 bug，記在這裡提醒之後任何要手動
    操作 Qdrant point id 的場合，一定要直接讀 `verse_archive_pipeline.py:199-201`
    現在的 `id_to_uuid()` 實作，不要憑印象/記憶重造一份
- 3.12.0 (2026-07-12): 修正分支 A 的 `to`/`cc` 收件人多時常是英文拼音名（沒有轉中文）
  - **根因**：`resolve_recipients()` 只有在收件人字串帶 `<email>` 時才能查 `email_mapping`
    轉中文名。但分廠/子公司員工的帳號用**英文** Domino canonical name 註冊（例如
    `Chun-Hua Huang/elfc1/everlight`，`everlight`=台灣永光化學、`elfc1`=一廠代碼），
    這種人 Verse 畫面上只顯示純文字姓名、完全沒有 `<email>` 可查，全都被原樣保留英文。
    用使用者手動操作＋多輪 Playwright 腳本實測驗證：`.collapsed-recipient` 不是展開
    按鈕（點擊前後內容不變）；點收件人姓名本身會跳出「名片卡」，背後其實是打一支標準
    iNotes API（`POST .../iNotes/Proxy/?EditDocument&Form=s_ValidationJson`），
    回應裡的 `altFullName`（`CN=黃俊華/OU=一廠/O=永光化學`）跟 `internetAddress`
    （`chunhua@ecic.com.tw`）就是要的中文名跟真正 email
  - **修法**：不用逐一點名片卡（一人一次點擊+等待，收件人多會很慢），改成主動批次呼叫
    這支 API——`resolve_canonical_names_via_api()` 用分號一次丟多個 canonical name，
    `resolve_unresolved_canonicals()` 幫每則訊息把還沒解析過的名字補查一次，結果累積
    進整個 pipeline run 共用的 `name_directory`，同一封信/同一次 run 裡重複出現的人
    不用再查第二次。這支 API 需要 `X-IBM-INotes-Nonce` header（頁面 meta
    tag/JS 全域變數都找不到固定來源），改成被動攔截 Verse 自己偶爾觸發的
    `s_ValidationJson` 請求（不一定跟正在處理的這封信有關）取得 nonce 值再重用，
    不用自己額外觸發
  - `extract_message_block()`/`extract_header_fields()` 新增抓每個收件人姓名
    `.socpimNameBtn` 的 `socpimnameemail` 屬性（`name_canonicals`），供上面兩支
    函式對照用；沒有 `@` 的才是需要查的 canonical name，有 `@` 的直接當 email
  - 已用真實信件（UNID `36C59C4FA5916F2348258E2F0022A091`，To/Cc 共 40+ 人）端對端
    驗證：整封信只多打一次批次請求（一次解析 43 人），第二則訊息完全重複利用第一則
    累積的結果、零額外請求；除了「一廠環保課 [C1710]」這種群組/mail-in 資料庫本來就
    查無個人中文名之外，其餘收件人全部正確轉成中文
  - **不在這次範圍內**：`resolve_recipients()` 對真正查無資料的人（Domino 也解析不出來，
    例如真的外部廠商）目前還是原樣保留英文、不會呼叫 `track_unknown_contact()` 記錄——
    這個跟 `resolve_sender()` 的追蹤機制不對稱的缺口是分開的問題，之後有需要再處理
- 3.11.0 (2026-07-12): 補上 `update_external_contacts.py`（尚未實作）設計缺口——
  重新 retain 前要先讀回舊 tags/metadata
  - 這支腳本本身還沒寫，這次只是修正「未知聯絡人確認機制」第 4 點的設計文字：
    原本只寫「重組 content 用同一個 document_id 重新 retain」，沒提到 `tags`/
    `metadata` 要一併帶著送——Hindsight 的 `retain()` 是整段覆蓋不是 merge，
    真的照原文字實作的話，姓名改對了，但 `tags=["mail"]`（3.10.0 剛加回來的來源
    標籤）跟 `metadata` 裡的 `thread_id`/`reply_to_unid` 這些欄位會直接消失
  - 補上正確順序：先 `get_document(document_id=unid, bank_id="EID")` 讀回舊
    `tags`/`document_metadata`，合併新姓名後再整包（`tags`+`metadata`）一起送
    `retain()`。已用現有一筆真實資料實測 `get_document()` 回傳裡確實有這兩個
    欄位可讀，不是憑空假設
- 3.10.0 (2026-07-12): 重新加回 `tags=["mail"]`（來源標籤，非 proj 分類）+ 新增
  Hindsight directive 說明 thread_id/reply_to_unid 語意
  - **背景**：3.7.0 把 `tags` 整個移除過（當時是為了拿掉沒在用的 `proj:` 分類佔位
    值）。這次重新檢視 `reflect()`/`recall()` 的 schema，發現它們都支援
    `tags`/`tags_match` 參數可以過濾記憶來源——實測「陰井雨水溝巡視結果」這個
    `reflect()` 查詢時，`search_observations` 撈回一堆不相關的 `hcl-approval`
    tag 資料（加班/未刷卡卡等簽核記錄），因為同一個 `EID` bank 裡混了其他 skill
    寫入的資料，沒有 tag 可以區分來源
  - **`HindsightClient.retain()` 加回 `tags` 參數**：`verse_archive_pipeline.py`
    的 retain 呼叫現在帶 `tags=["mail"]`——**跟 proj 分類是兩件事**：`mail` 是
    「資料來源」標籤（給 `reflect(query, tags=["mail"])` 過濾用），之後要做的
    `proj:xxx` 才是「專案分類」標籤，兩者不衝突，屆時可以 `tags=["mail", "proj:xxx"]`
    並存
  - **新增 Hindsight directive**：`create_directive()` 在 `EID` bank 建立一則
    directive（`tags=["mail"]`），內容說明 `metadata.thread_id`（同討論串）/
    `reply_to_unid`（回覆關係）/`sent_date`（時間順序）這三個欄位的語意，讓
    `reflect()` 推論討論串脈絡時有明確依據可循，不用單靠模型自己從欄位名稱猜語意
    （之前小規模測試證實模型「大致猜得到」，但沒有保證，這次補上正式說明）
- 3.9.0 (2026-07-12): 修正西式「姓, 名」收件人被逗號誤拆成兩人的 bug
  - **問題**：Verse 顯示收件人常見西式「Lastname, Firstname」格式（例如「Hsieh, Tata」
    「Yamashita, Yuutoku」），這個逗號是名字的一部分。但 `resolve_recipients()`
    跟組 `to_raw`/`cc_raw`（EML 信頭用）的地方都只是單純 `raw.split(',')`，逗號
    不分青紅皂白全部當成收件人分隔符，「Hsieh, Tata `<email>`」會被切成「Hsieh」
    （沒有 email 的假收件人）跟「Tata `<email>`」兩截。RAG/Hindsight 那邊只是姓名
    顯示不美觀，但 **EML 的 `To:`/`Cc:` 信頭**這樣寫不符合 RFC 5322——Gmail（或任何
    標準郵件軟體）解析信頭時看到未加引號的逗號，一樣會把這個人拆成兩個收件人，
    是使用者實際在 Gmail 上看到的問題
  - **新增 `_split_recipient_entries()`**：初步用逗號切開後，如果某一段是「不含
    空白的純英文單詞」（像獨立姓氏 Hsieh、Yamashita）且沒有 email，同時緊接著的
    下一段有 `<email>`，判定是被誤切的同一人，合併回去。只處理這種單一英文單詞
    的情況——中文姓名、多字英文全名（例如「Yao-Chung Liu」本身就含空白）不會被
    合併，避免把兩個不同人誤判成同一人（這是啟發式判斷，不是 100% 語意理解）
  - **新增 `quote_recipient_header()`**：組 `to_raw`/`cc_raw`（EML 信頭用）時，把
    顯示名稱本身含逗號的收件人用雙引號包起來（`"Hsieh, Tata" <email>`），變成合法
    的 RFC 5322 位址格式，Gmail 才會正確解析成一個收件人而不是拆成兩個
  - `resolve_recipients()`（分支 A 用）改用 `_split_recipient_entries()` 取代原本的
    `raw.split(',')`，姓名顯示也一併修正
  - 已用實測資料驗證：「Yamashita, Yuutoku」「Hsieh, Tata」正確識別成單一收件人並
    加上引號；同時確認「Chun-Hua Huang, Yao-Chung Liu, 穆彥池 `<email>`」這種多個
    獨立收件人不會被誤合併
- 3.8.0 (2026-07-12): 寫入前先查 Qdrant，UNID 已存在就整段跳過（不覆蓋）
  - **新增 `already_indexed(unid)`**：寫 RAG/Hindsight 前先用
    `qdrant.retrieve(ids=[id_to_uuid(unid)])` 查這個 UNID 是否已經寫進 Qdrant，
    已存在就整段跳過（不呼叫 embedding、不 upsert、不 retain），只計進
    `rec["skipped_dup"]`。查詢失敗（例如 collection 還不存在）視為「還沒索引過」，
    正常走寫入流程
  - **原因**：原本 Qdrant upsert / Hindsight retain 都是用 `unid` 當 id 的
    idempotent 寫入，同一個 unid 重複寫入本來就不會產生重複記錄，只會覆蓋。但
    Hindsight 那筆記錄如果事後被人工整理過，重新 retain 會把整理過的內容蓋掉——
    改成直接跳過，不再覆蓋，才不會洗掉已經整理過的資料
  - **背景**：2026-07-12 用真實討論串「興忠行 永光-化學四廠 HVM 工程建造會議-會議
    記錄」實測驗證過 UNID 跨帳號一致（同一則訊息在黃樹瑆、穆彥池兩人信箱裡的 UNID
    完全相同），這是之後要合併同事信箱信件進同一個 Qdrant/Hindsight 時，dedup 判斷
    可以直接信賴 UNID 的前提；細節見對話記錄與 memory（`hcl-verse-unid-cross-account`）
  - 這次只調整腳本本身（加上這個檢查），還沒有實際跑穆彥池的信箱——那是下一步
- 3.7.0 (2026-07-11): Hindsight retain 移除 `tags`、metadata 移除 `cc`
  - **移除 `tags`**：`HindsightClient.retain()` 簽名拿掉 `tags` 參數，request 不再帶
    這個欄位；呼叫端也拿掉 `tags = ["source:verse"]` 這行。之後要補 proj 分類時，
    看情況要嘛重新加回 `tags` 參數傳 `proj:xxx`，要嘛改用 `metadata` 存分類，屆時再定
  - **metadata 移除 `cc`**：Hindsight `metadata` 只剩
    `{subject, from_email, from_name, to, thread_id, unid, reply_to_unid, sent_date}`，
    不再包含 `cc`。**只影響 Hindsight**，Qdrant payload 跟分支 B 的 `cc_raw`（EML 用）
    都沒有動
- 3.6.0 (2026-07-11): 分支 B（EML/Gmail）端對端驗證通過——EML 搬到部門共用網路磁碟、
  檔名改成 UNID、Gmail 上傳實測成功
  - **`EML_OUTPUT_DIR`**：新增常數，`.eml` 跟 `ATTACHMENTS_DIR`（附件另存）都從本機
    `~/verse-export` 搬到部門共用網路磁碟
    `\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\eml`（可用同名環境變數覆寫）；
    `verse_upload_gmail.py` 的 `eml_folder` 預設值同步改成這個位置
  - **EML 檔名改成 `{unid}.eml`**：原本用 `{主旨}_{序號:02d}_{寄件者}` 組檔名，改用
    UNID——原因是主旨/序號/寄件者組出來的檔名不好查詢、也沒有實質用途，UNID 本身
    就是唯一 key，方便直接回頭比對 Qdrant/Hindsight 裡的同一筆資料
  - **Gmail OAuth 憑證補齊**：這台機器原本完全沒有 `~/Documents/eml to gamil/`
    這個資料夾，`credentials.json`/`token.json` 從別的地方拿過來放好（`token.json`
    含 `refresh_token`，不用重新跑一次瀏覽器同意畫面）
  - **發現並排除一個非程式碼的環境問題**：自動化工具第一次嘗試連線
    `\\10.11.1.40\...` 這個網路磁碟時，`Test-Path`/`os.makedirs` 回報連不上，但
    使用者自己的檔案總管完全正常——不是帳號密碼或分享權限問題，是 SMB session
    在該次執行環境裡還沒建立；跑一次 `net use "\\10.11.1.40\..."` 觸發連線後
    就恢復正常，之後的寫入/讀取測試都成功
  - **真實端對端驗證**：拿 04Done 真實討論串「巡視各棟內外陰井、雨水溝結果」
    （2 則訊息，蔡道明回覆洪建旭，5 個附件）完整跑過分支 B 全流程——訊息拆分、
    `reply_to_unid` 配對正確（蔡道明 → 洪建旭）、5 個附件全部正確命名（修正前
    3 個顯示成「Download file」）、EML 以 UNID 命名存到網路磁碟、實際上傳到 Gmail
    成功（標籤 `Notes_Import`，搬到 `eml_done`），且 `Message-ID`/`In-Reply-To`
    正確讓 Gmail 自動把兩則訊息顯示成同一對話串（使用者親自在 Gmail 裡確認過）
  - 分支 A（RAG/Hindsight）已由其他流程處理並記錄在 3.3.0～3.5.1，本次不動
- 3.5.1 (2026-07-11): `verse_archive_pipeline.py` 的 `retain()` 明確帶 `bank_id="EID"`
  - 之前 `verse_archive_pipeline.py` 呼叫 Hindsight `retain` 完全不帶 `bank_id` 欄位，
    靠 server 端隱式預設值（實測是 `EID`）；改成跟 `gmail_backfill.py` 一樣，在
    `HindsightClient.retain()` 簽名加上 `bank_id="EID"` 預設值並放進 request payload，
    兩支腳本寫入哪個 bank 不再依賴 server 端設定，改成程式碼裡明講
- 3.5.0 (2026-07-11): Windows 主控台編碼修復 + 附件另存 + Hindsight bank 修正
  - **Windows 主控台 UnicodeEncodeError 修復**：`verse_archive_pipeline.py`/
    `verse_upload_gmail.py` 一開始都補上 `sys.stdout/stderr.reconfigure(encoding='utf-8')`
    （非 utf-8 時才套用）。Windows 主控台預設用 cp950（Big5），印 `✓`/`✗`/`📧` 等符號
    會直接 `UnicodeEncodeError` 崩潰——這次在這台 Windows 機器上第一次實跑 04Done
    歸檔就踩到，正式歸檔跑到一半、RAG/Hindsight 都已經寫入成功才在印結果那行崩潰
  - **`verse_upload_gmail.py` 的 `GMAIL_DIR` 改用 `os.path.expanduser("~/Documents/eml to gamil")`**：
    原本寫死 `/Users/shuhsing/Documents/eml to gamil`（macOS 路徑），在 Windows 上
    找不到資料夾；`gmail_backfill.py` 有同樣的寫死路徑，暫未跟著修（這次沒有用到）
  - **附件另存**：新增 `save_attachments()`，每則訊息下載到的附件除了照舊內嵌進
    `.eml`，另外存一份到 `~/verse-export/attachments/`（全部平放同一個資料夾，
    檔名前綴 unid 避免同名衝突），同一份下載結果重複使用（`m["_attachment_data"]`），
    不會為了另存而多打一次下載請求
  - **Qdrant payload 新增 `attachments` 欄位**：`[{name, path}, ...]`，`path` 指向上面
    另存的實體檔案位置；原本 Qdrant/Hindsight 完全沒有記錄附件檔名或位置，只有
    `.eml` 裡有
  - **Hindsight bank 修正**：`gmail_backfill.py` 的 `retain(bank_id="shuhsing")` 預設值
    改成 `"EID"`——`shuhsing` 是舊 Mac 機器的帳號名稱，這台 Windows 機器的 Hindsight
    server 預設 bank 是 `EID`（`verse_archive_pipeline.py` 本來就沒有明講 bank_id，用
    server 端預設值，已驗證能正常寫入，不用改）；文件裡「retain 到 shuhsing bank」
    的描述一併更正
  - 用 04Done 真實討論串「RE: ECIC Fab.4 CG-6000 series製造設備建設工事の進捗」
    （8 則訊息、10 個附件）端對端驗證：RAG 8/8、Hindsight 8/8 成功，附件另存 + Qdrant
    payload 記錄位置皆正常
- 3.4.2 (2026-07-11): 修正 Qdrant 實際位置 —— 跑在 10.11.1.40，不是 WSL localhost
  - 四支腳本（`verse_archive_pipeline.py`/`verse_rag_search.py`/`verse_query.py`/
    `gmail_backfill.py`）的 `QDRANT_URL` 預設值從 `http://localhost:6333` 改成
    `http://10.11.1.40:6333`；環境目前沒有設定 `QDRANT_URL` 環境變數覆寫，照舊預設值
    跑 RAG 那步會連錯地方
  - 原因：先前文件誤記成「跑在 WSL docker 容器」，之後排除 Qdrant 相關問題要往
    10.11.1.40 這台 Synology NAS 查，不是本機 WSL
  - port 沿用 Qdrant 預設 6333，未變
  - 補充確認：NAS 上是**常駐服務**，不像舊文件講的 WSL docker 容器那樣可能因
    重開機/WSL 重啟變成 Exited、需要手動 `docker start`——已知缺口章節原本那條
    「重開機可能要手動啟動」的提醒一併移除
- 3.4.1 (2026-07-11): 執行指令 `python3` → `python`
  - 這台機器的 `python3` 是壞掉的 Windows Store 別名（靜默失敗，exit code 49，
    沒有任何錯誤輸出），實測必須用 `python` 才能正常執行。「執行」章節所有指令
    範例（`verse_archive_pipeline.py`/`verse_upload_gmail.py`/`verse_rag_search.py`/
    `verse_query.py`/`gmail_backfill.py`，共 11 處）都改成 `python`，並加註在真正
    WSL/Linux 環境下 `python3` 才是正常對應指令，屆時可換回來
- 3.4.0 (2026-07-11): 修正 embedding port 誤判、`verse_rag_search.py` 的 API 相容性、
  4 支腳本的結果檔路徑/編碼
  - **Embedding server port 修正 8090 → 8081**：3.3.0 誤以為 8090（llama-server
    backend 直接監聽的 port）就是正確答案，但這個 port 背後有
    `jina-embed-idle.timer` 閒置 ~10 分鐘會自動關掉省 RAM——長時間跑 pipeline
    中途接不到會斷線。查了 WSL 上的 systemd 設定才發現真正的穩定入口是
    **8081**（`jina-embed.socket`，on-demand activation）：沒人用時 backend 是關的，
    一有連線 `proxy-relay.py` 會自動 `systemctl start jina-embed.service` 喚醒、
    等 healthy 再轉發，用完閒置一段時間又會被關掉——這才是設計上該接的埠。
    四支腳本的 `EMBEDDING_API_BASE` 預設值都改成 8081
  - **`verse_rag_search.py` 修正 `qdrant.search()` → `qdrant.query_points()`**：目前
    安裝的 qdrant-client 版本已經移除 `.search()`（`AttributeError`）。順便修正
    結果欄位對應——原本讀的是 `payload.get("from")`/`payload.get("snippet")`，
    但實際 payload 存的 key 是 `from_name`/`from_email`/`body`，就算 API 呼叫修好了
    結果也會全部是空字串，一併改成讀正確的 key（加上 `sent_date`/`unid`/
    `reply_to_unid`）
  - **`OUTPUT_FILE` 路徑改用 `tempfile.gettempdir()`**：`verse_rag_search.py`/
    `verse_query.py`/`verse_upload_gmail.py` 原本寫死 `/tmp/...`，在非 WSL 的原生
    Windows Python 下該路徑不存在，會直接 `FileNotFoundError`；改成
    `os.path.join(tempfile.gettempdir(), ...)`，跟 `verse_archive_pipeline.py`
    原本的寫法一致
  - **`OUTPUT_FILE` 寫入補上 `encoding="utf-8"`**：4 支腳本（含
    `verse_archive_pipeline.py`）寫結果 JSON 時都沒指定編碼，Windows 預設
    codepage（如 cp950）遇到 Notes 信件常見的不換行空白等字元會直接
    `UnicodeEncodeError`——代表正式歸檔全部跑完（Verse 爬取 + Qdrant/Hindsight
    寫入）後，可能在最後寫摘要這一步才崩潰。全部補上 `encoding="utf-8"`
  - 這幾個修正都用實際指令驗證過（`query_points()` 呼叫成功、暫存路徑+UTF-8
    寫入獨立測試通過），只有最後一次語意搜尋因為 embedding server 剛好被
    idle-watchdog 關掉、重啟後才用 8081 重新測過一次確認成功
- 3.3.0 (2026-07-11): 分支 A 首次活體端對端驗證，修正兩個擋住 100% 寫入的 bug
  - **Qdrant 向量維度修正 1024 → 2048**：實測發現本地 jina-embeddings-v4 server 實際
    回傳 2048 維，不是先前假設的 1024，導致 collection 建立時維度不合、upsert 100%
    失敗（查證時 collection 是空的，`points_count: 0`，代表這個 bug 從 3.1.0 改用本地
    embedding 後就沒讓任何一筆資料寫進去過）。已重建 `verse_emails` collection 為
    2048 維，`verse_archive_pipeline.py`/`gmail_backfill.py` 的 `VECTOR_SIZE` 同步更新；
    API 的 `dimensions` 參數在這個 llama-cpp-server 版本不會生效（不支援 Matryoshka
    截斷），所以是用完整 2048 維，不是截斷
  - **Embedding server port**：一開始誤判成 8090（llama-server backend 本身監聽的
    port），後來發現正確答案是 **8081**（見 3.4.0 修正）
  - **Hindsight 拒絕 `reply_to_unid: null`**：討論串裡最原始那則訊息（沒有回覆對象）
    的 `reply_to_unid` 是 `None`，直接傳給 Hindsight metadata 會被 pydantic validation
    擋下來（`Input should be a valid string`）。修正成 `reply_to_unid` 為空值時整個
    省略這個 metadata key，不傳 null；同時修正 `retain()` 呼叫端只看有沒有拋出
    Python exception就當作成功的問題——現在會額外檢查回應內容裡有沒有
    `validation error` 字樣，避免把實際被拒絕的寫入誤判成功
  - **端對端驗證**：拿 04Done 真實討論串「MES與Intouch連線問題」（3 則訊息）修正後
    重跑，Qdrant 3/3、Hindsight 3/3 全部成功。驗證了兩種讀取效果：Qdrant 語意搜尋
    正確依相關度排出三則訊息；Hindsight `reflect` 能正確整合三則訊息（body 已各自
    砍過引用歷史）綜合回答根本原因/短期方案/長期方案，寄件者用姓名不用 email，
    `reply_to_unid` 在 raw memory 的 metadata 裡正確可見
  - 順帶發現但**尚未修正**：`verse_rag_search.py` 用的 `qdrant.search()` 在目前安裝的
    qdrant-client 版本已被移除（應改用 `query_points()`），見「已知缺口」章節
- 3.2.0 (2026-07-11): 定案「兩分支」設計——RAG/Hindsight 用清完版，EML/Gmail 用完整版
  - **分支拆開**：同一則訊息的原始內容明確拆成兩份加工結果，各走各的用途，不再共用：
    - 分支 A（RAG/Hindsight）：`body`（`clean_body_and_identify()`，砍引用歷史）、
      `to`/`cc`（`resolve_recipients()`，解析成姓名，不含 email）
    - 分支 B（EML/Gmail）：`eml_body`（`_strip_ui_noise()`，只剝 UI 雜訊，**保留**引用
      歷史）、`to_raw`/`cc_raw`（`substitute_me()`，保留真實 email，Gmail 匯入需要）
  - **`reply_to_unid` 前後文機制**：`quote_stripper.strip_quoted_history_with_identity()`
    砍引用歷史前先抽出「被引用的是誰、什麼時候」，`match_reply_to()` 拿去跟同批訊息比對，
    寫入 `reply_to_unid`——分支 A 的 body 雖然砍了引用，但靠這個指標保留討論串前後文
    關係；同一組配對結果也給分支 B 拿去組 EML 的 `Message-ID`/`In-Reply-To`/`References`
  - **EML 改成逐則**：從「一個討論串一個 EML（整串塞在一起）」改成「**每則訊息各自
    一個 `.eml`**」，帶標準 `Message-ID`（UNID）/`In-Reply-To`（`reply_to_unid`），
    Gmail 匯入後靠標準信頭自動重建討論串，不用自己另外處理；附件也改成照 UNID 分給
    對應那則
  - **原因**：分支 A 需要去重（避免討論串裡的舊內容透過引用重複灌爆 Hindsight），但
    去重會丟失「這則回覆哪一則」的關係；分支 B 需要保留信件原貌供人工回溯、且要能被
    Gmail 正確匯入。兩邊需求互斥（一個要砍、一個不能砍），所以拆成兩條獨立分支，只
    共用同一組 `reply_to_unid` 配對結果銜接兩邊
  - 詳細設計討論見對話記錄；已用 04Done 真實討論串「MES與Intouch連線問題」（3 則訊息）
    端對端驗證 `reply_to_unid` 配對正確、EML 內容完整保留引用歷史、Message-ID/
    In-Reply-To 正確串接（測試腳本另存，未寫入正式 Qdrant/Hindsight、未搬移信件）
- 3.1.0 (2026-07-11): RAG embedding 改用本地模型，不再需要 OpenAI API key
  - `verse_archive_pipeline.py` / `verse_rag_search.py` / `verse_query.py` / `gmail_backfill.py`
    的 `text-embedding-3-small`（OpenAI 雲端）全部改成本地 llama-cpp-server 跑的
    `jina-embed`（`jina-embeddings-v4-text-retrieval-Q4_K_M.gguf`），`OpenAI()` client
    只是借用 SDK 打 OpenAI-compatible API，`base_url` 指到 `http://localhost:8082/v1`，
    `api_key` 只當佔位字串（本地伺服器不驗證）
  - 向量維度 1536 → 1024（jina-embeddings-v4），四支腳本的 `VECTOR_SIZE` 與 Qdrant
    collection `verse_emails` 需同步；新增 `EMBEDDING_API_BASE`/`EMBEDDING_MODEL`
    環境變數可覆寫
  - 原因：pipeline 一直卡在要求使用者提供 OpenAI key，但公司內部已有本地 embedding
    server 可用，改用它就完全不需要外部 API key
  - 過程中發現 Qdrant（跑在 WSL docker 容器 `qdrant`）目前是 Exited 狀態，需要先啟動
    才能真的寫入——這是下一個要排除的障礙，跟本次改動無關
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
