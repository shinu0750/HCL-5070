---
name: hcl-move-meeting
description: >
  HCL Verse 會議通知接受回覆歸檔自動化。當用戶提到整理會議接受回覆、
  會議通知歸檔、把已接受的會議移到資料夾、整理會議信件時使用此 skill。
  自動掃描收件匣找出所有「已接受：」開頭的信件（同仁接受會議邀請的回覆），
  逐一移到「會議通知」資料匣。
version: 1.2.3
---

# HCL Verse 會議通知接受回覆歸檔

自動將收件匣中同仁回覆接受的會議通知信件（主旨含「已接受：」）移到「會議通知」資料匣。

## 搜尋關鍵字

腳本使用多關鍵字搜尋（`KEYWORDS` list），目前包含：
- 已接受: （半形冒號）

如需新增關鍵字，修改腳本的 `KEYWORDS` list 即可。

## 執行

直接執行腳本：

```bash
python3 ~/.claude/skills/hcl-move-meeting/hcl_move_meeting.py
```

腳本會：
1. 登入 HCL Verse（憑證來自 `~/.hermes/.env`）
2. 從頂部逐頁捲動掃描收件匣（兼容 virtual scroll，120 封以上 inbox 不會漏掃）
3. 累積去重所有含「已接受：」的 treeitem（含計數 > 1 的討論串）
4. 逐一點擊 → 點資料夾 icon → 輸入 `會議通知` → 移動
5. 輸出結果到 `/tmp/hcl_move_meeting.json`

## 結果呈現

執行完成後，告知用戶：

```
共找到 N 封「已接受：」信件
✓ 成功移動：N 封
✗ 失敗：N 封（若有，列出主旨）
```

## 寫入 Hindsight

呈現結果後，呼叫 `mcp__hindsight__sync_retain` 將本次執行摘要寫入 Hindsight：

- content：包含執行日期、移動封數、移動信件清單（寄件者 / 主旨）
- bank：`shuhsing`
- metadata：`{"type": "hcl_move_meeting", "date": "YYYY-MM-DD"}`

範例內容：
```
2026-05-30 會議接受回覆歸檔
移動 3 封到 會議通知：
- 康睜芳 / 已接受: 3D雷射掃描儀 產品交付跟驗收測試
- 穆彥池 / 已接受: 3D雷射掃描儀 產品交付跟驗收測試
- 李鎮宇 / 已接受: 3D雷射掃描儀 產品交付跟驗收測試
```

若移動 0 封則不需寫入。

## 關鍵技術細節（供除錯參考）

- **移動按鈕 selector**：
  ```
  div.sticky-header > div.action-bar.collapse-stage-0.action-tray-populated > button.action.pim-move-to-folder.icon
  ```
  - 必須包含 `collapse-stage-0` 才能精確命中，避免選到頁面上其他隱藏的同名按鈕
  - 單封信與討論串皆適用此 selector
- **虛擬捲動**：Verse 收件匣是 virtual scroll — DOM 只保留可見窗 ± buffer，捲過的會被回收。
  腳本從頂部逐頁捲動（每頁 85% clientHeight，留 15% 重疊），每頁掃 DOM 累積符合主旨，
  捲到底 OR 連續 3 頁無新主旨才停。120+ 封 inbox 不會漏。

## 常見問題

### 找不到資料夾 icon（error_no_button）
- 可能是頁面還沒載入完，腳本已超時
- 解法：確認 HCL Verse 網路連線正常，重新執行

### 找不到信件（not_found）
- 信件可能已被手動移走，或主旨不含任何關鍵字
- 正常情況，忽略即可

## Changelog

- 1.2.3 (2026-06-10): 同步 hcl-move-construction 的討論串 selector 修正 —
  討論串資料夾 icon 在 `div.sticky-header > div > button` 路徑下（沒有 action-bar class），
  新增此 selector 作為 fallback，避免討論串回報 `error_no_button`。
- 1.2.2 (2026-06-10): 修正 `move_to_folder` 假性成功 bug — 舊版用 `:visible.first` 點下拉項目，
  中文資料夾名稱（如「會議通知」）過濾沒生效時會選到第一個無關項目，導致回報 moved 但實際沒移動。
  修法：(1) `.fill("")` 清空輸入框再 `.type(name, delay=50)` 確保 React onChange 觸發；
  (2) 用 `:has-text('{TARGET_FOLDER}')` 明確比對資料夾名稱；
  (3) 移動後檢查 popup 是否關閉，未關閉就補按 Enter，仍未關閉回報 `error_popup_stuck`。
  實測：舊版跑兩次同樣掃到 3 封（沒真移動）；新版第一次跑完，第二次掃到 0 封確認移動成功。
- 1.2.1 (2026-06-10): 移除 `collect_subjects` 的「連續 3 頁無新主旨就停」早停邏輯 —
  符合關鍵字的信件可能稀疏分布在中段或尾段（實測 120 封 inbox 中 3 封「已接受:」在第 16 頁才出現），
  前幾頁沒命中不代表後面沒有，必須真的捲到底才能下結論。改為「只在捲到底或達 max_pages=80 上限時停」。
- 1.2.0 (2026-06-10): 收件匣掃描兼容 Verse virtual scrolling — 舊版「跳到底再 locator.all()」
  只能看到當下渲染的 ~30 封，120 封 inbox 會漏大半。改為「從頂部逐頁捲動 + 累積去重」，
  連續 3 頁無新主旨或捲到底才停；移動階段的 `find_item_in_inbox` 也改用同樣的捲找邏輯。
- 1.1.0 (2026-06-03): Hindsight sync_retain 指定寫入 shuhsing bank
- 1.0.0 (2026-06-03): 納入版本管理，初始版本
