---
name: hcl-verse-RAG
description: >
  HCL Verse 信件歸檔 pipeline。當用戶提到歸檔 Verse 信件、處理 04Done 信件、
  把 04Done 的信存成 EML、建立 Verse RAG 索引、整理已完成信件、
  把信移到 domdom、把 Verse 信件上傳到 Gmail 時使用此 skill。從「04Done」資料夾逐封：
  抓全文+附件 → 建 RAG 索引(Qdrant) + 存成 EML → 移到「domdom」→ 上傳 Gmail(Notes_Import)。
version: 2.3.0
---

# HCL Verse 信件歸檔 Pipeline

從「**04Done**」資料夾逐封處理已完成的信件，一次點開同時做兩件事
（建 RAG 索引 + 匯出 EML），處理完移到「**domdom**」資料夾。

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
- `--headful` 顯示瀏覽器視窗（除錯用）

建議用 Bash `run_in_background: true` 執行，搭配 Monitor 監看
（成功標記：`結果已寫入`；失敗標記：`✗` / `Traceback`）。

**接著自動上傳 Gmail**（正式歸檔後一定要執行，`--no-move` 試跑則略過）：

```bash
python3 ~/.claude/skills/hcl-verse-RAG/verse_upload_gmail.py
```

把 `~/verse-export/` 的 EML 批次 import 到 Gmail 標籤 `Notes_Import`，
成功後搬到 `~/Documents/eml to gamil/eml_done/`。沿用既有 OAuth 憑證/token
（`~/Documents/eml to gamil/`），有自己的 log 去重，重跑只補上次失敗的。

## 流程（每封）

1. 登入 Verse → `open_folder("04Done")` 進指定資料夾
2. 取清單**最上面那封**點開 → 抓 header(from/to/cc/date/labels) + 全文(清 UI 雜訊) + 附件連結
3. **① RAG**：`text-embedding-3-small` → upsert 到 Qdrant collection `verse_emails`
4. **② EML**：下載附件(`verify=False`) → 打包成 `.eml` 存到 `~/verse-export/`
5. **③ Hindsight retain**：用 `project_keywords.match_projects()` 判定 `proj:` tag → `retain` 到 `shuhsing` bank
   - `document_id` = `hash(from|subject|date)`（idempotent）
   - `tags` = `[source:verse, proj:xxx]`
   - `metadata` 含 `eml_path`、`thread_id`、`label_ids`、`sent_date`
6. **④ 移動**：按「Move to folder」→ 輸入 `domdom` → 該信移出 04Done
7. 那封消失，回到步驟 2 處理下一封最上面的，直到清空或達上限
8. 全部歸檔後 **⑤ 上傳 Gmail**：`verse_upload_gmail.py` 批次 import → 標籤 `Notes_Import` → 搬到 eml_done

**安全閥**：記已處理的 email id；若最上面那封是已處理過的（代表移動失敗它還在頂部）
或單封移動失敗，立即停止，避免無限迴圈或重複索引。

## 結果呈現

讀取兩個結果檔：
- 歸檔：`/tmp/verse_archive_pipeline_result.json`
  `{source, target, no_move, archived_date, sent_date_range, processed, rag_ok, hindsight_ok, moved, emails[]}`
- 上傳：`/tmp/verse_upload_gmail_result.json`
  `{total, uploaded, failed, label, done_folder, results[]}`

呈現格式：

```
✓ 從 04Done 歸檔 N 封 → domdom
  RAG 索引：N 成功 / Hindsight：N 成功
  EML：~/verse-export/（含附件）
  Gmail：上傳 N 封到 Notes_Import（搬到 eml_done）
  [1] [5/29] PharmaSuite 專案週報（RAG ok, Hindsight ok (proj:PharmaSuite/MES), 3 附件, moved, gmail ✓）
  [2] ...
✗ 失敗：列出 rag/eml/hindsight/move/gmail 任一失敗的信件主旨
```

## 寫入 Hindsight

每封信在歸檔時自動 retain（不需手動補寫）。關鍵欄位：

- `document_id` = `hash(from|subject|date)`（idempotent，重跑不重複）
- `timestamp` = `sent_date`（信件真實寄件日，非歸檔日）
- `content` = 清乾淨的信件全文（不預摘要，讓 Hindsight 自行抽取 facts）
- `tags` = `[source:verse, proj:xxx]`（proj 由 `project_keywords.py` 關鍵字比對決定）
- `metadata` = `{subject, from, thread_id, eml_path, label_ids, sent_date}`

> `thread_id` 只進 metadata（搜尋折疊用），不扛記憶責任。

## 技術細節（除錯參考）

- 信件清單 selector：`.seq-msg-row`（列文字含 `From / Subject / Message abstract`；討論串多一行 `Count\nN`）
- 閱讀窗格：`.preview-container`
- 資料夾導航：左側 `[role="treeitem"]:has-text("04Done")`；Inbox 才有專屬 class `.inbox`
- 移動鈕：`button.action.pim-move-to-folder.icon`（取**可見**的那個）。
  注意資料夾檢視的 action-bar 是 `action-bar collapse-stage-0`，**沒有** `action-tray-populated`
  （那是 Inbox 檢視才有）—— 不能用父層 class 比對，要直接鎖定按鈕本身
- 移動 popup：`div.folder-tray-float.show`，輸入 `input.folder-search-input` 後選
  `[role='treeitem']:visible:has-text('domdom')`（精準比對，避免選錯同名項目）
- 附件連結：`$File/...?OpenElement`（Domino 標準 URL）；下載需 `verify=False`（公司內部憑證）
- 附件命名：`a.innerText` 若為空或裸副檔名(pdf/xlsx...)，改從 URL 的 `FileName=` 取真檔名
- 日期：`.pim-mailread-sentdate`（取最長那行）→ `normalize_sent_date()` 正規化成 ISO；
  缺年份時推算（月份比今天超前 >7 天 → 去年）。避開 `[class*="ate"]`（會混進行事曆 widget 雜訊）
- Embedding：長討論串可能超過 8192 token 上限 → `get_embedding()` 用 tiktoken 截斷到 8000 token
- Qdrant：`http://localhost:6333`，collection `verse_emails`，向量 1536 維
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

## Changelog

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
