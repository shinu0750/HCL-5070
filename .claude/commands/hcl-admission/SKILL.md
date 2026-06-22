---
name: hcl-admission
description: >-
  HCL Verse 訪客入廠申請審核自動化。當用戶提到訪客入廠申請、入廠審核、
  入廠申請待審核、幫我審核入廠、核准入廠申請時使用此 skill。
  自動登入 Verse 找出「訪客入廠申請 待審核」信件，點開信中 LEAP 表單鏈結，
  讀取表單內容回報用戶，經確認後按「核准」。
---

# HCL Verse 訪客入廠申請審核

登入 Verse → 找「訪客入廠申請」待審核信 → 點開 LEAP 表單 → 回報內容 → 按「核准」。

## 執行

```bash
python3 ~/.claude/skills/hcl-admission/hcl_admission.py
```

使用 Bash 的 `run_in_background: true` 執行，搭配 Monitor 監看輸出
（成功標記：`結果已寫入`；失敗標記：`✗` / `Traceback`）。
腳本結束後瀏覽器保持開啟，用戶關閉視窗腳本才退出。

腳本流程：
1. 登入 HCL Portal（憑證來自 `~/.hermes/.env`）→ 進入 Verse 收件匣
2. 捲動收件匣到底，搜尋含「訪客入廠申請」的 treeitem
3. 逐封處理（迴圈，上限 20 封，以主旨去重）：
   - 點開信件 → 點擊信中 `leap.ecic.com.tw` 鏈結（會開新分頁），讀取表單內容
   - 按「核准」（exact match，避免誤點「取消申請」「駁回」），處理可能的確認對話框
   - 關閉表單分頁 → 回收件匣，**把該信移到「Sign」資料匣**（重用 hcl-move-construction 移動邏輯）
   - 處理下一封
4. 結果輸出到 `/tmp/hcl_admission.json`
   （`{total, approved, moved, results[]}`，每封含 subject/status/move_status/form_text/after_text，
   成功時 after_text 含「已順利提交您的資料」）

注意：只有核准成功（approved）的信才會移到 Sign；核准失敗的信留在收件匣供人工處理。

## 結果呈現

依規範用表格呈現表單詳細內容（入廠日期、來訪單位、洽公事由、訪客名單、
受訪單位、承辦人員等），以及核准結果：

```
| 項目 | 結果 |
|------|------|
| 表單 | 入廠申請_801 — XXX N人，YYYY/M/D |
| 動作 | 按下「核准」 |
| 系統回應 | 已順利提交您的資料 |
```

## 寫入 Hindsight

核准成功後，呼叫 `mcp__hindsight__sync_retain` 寫入摘要：

- content：執行日期、來訪單位、入廠日期、人數、訪客名單、核准結果
- bank：`shuhsing`
- metadata：`{"type": "hcl_admission", "date": "YYYY-MM-DD"}`

找到 0 封待審核信則不需寫入。

## 關鍵技術細節（供除錯參考）

- **LEAP 表單鏈結**：信中 `a[href*="leap.ecic.com.tw"]`，點擊用
  `ctx.expect_page()` 接新分頁；若無新分頁則同頁導航
- **核准按鈕**：`get_by_role("button", name="核准", exact=True)`，
  fallback `button:text-is("核准"), input[value="核准"]`
- **表單頁**：`leap.ecic.com.tw/volt-apps/.../launch/index.html?form=F_Form1&id=...`
  （HCL Volt 表單），需先有 Portal session 才能存取
- **虛擬捲動**：收件匣同 hcl-move-construction，需 JS scroll 到底載入全部信件

## 常見問題

### 找不到信（count=0）
- 信件可能已被審核或移走，正常情況，回報用戶即可

### 找不到 LEAP 鏈結
- 需先點開信件讓閱讀窗格載入；確認信件確實為系統自動轉發的審核通知

### 核准後沒有「已順利提交」
- 檢查 after_text 是否有駁回原因必填等錯誤訊息，截圖回報用戶

## 待開發

- **申請人是本人的情況**：表單需填寫較多資訊（非單純核准），遇到實際案例時再開發，
  屆時新增為腳本的另一模式

## Changelog

- 1.2.1 (2026-06-03): Hindsight sync_retain 指定寫入 shuhsing bank
- 1.2.0 (2026-06-03): 核准成功後自動把信移到「Sign」資料匣
- 1.1.0 (2026-06-03): 支援多封信迴圈處理 — 逐封核准後回收件匣重掃，
  直到沒有待審核信（上限 20 封）；以主旨去重避免重複處理；
  結果 JSON 改為 `{total, approved, results[]}`
- 1.0.0 (2026-06-03): 初始版本 — 找信、開表單、核准完整流程已實測成功
  （台灣洛克威爾 2026/6/9 入廠申請）
