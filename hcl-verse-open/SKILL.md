---
name: hcl-verse-open
description: >-
  開啟瀏覽器登入 HCL Verse 信箱。當用戶說「開啟 Verse」、「登入 Verse」、
  「打開信箱」、「幫我登入信箱」時使用此 skill。自動開啟瀏覽器、
  登入 HCL Portal、進入 Verse 收件匣，然後保持瀏覽器開啟供用戶手動操作。
---

# HCL Verse 登入

開啟瀏覽器 → 登入 HCL Portal → 進入 Verse 收件匣 → 保持開啟。

## 執行

以背景方式執行（瀏覽器要保持開啟，腳本會等到用戶關閉視窗才結束）：

```bash
python3 ~/.claude/skills/hcl-verse-open/hcl_verse_open.py
```

使用 Bash 的 `run_in_background: true` 執行，看到輸出
`✓ 已登入並開啟 Verse 信箱` 即代表成功，告知用戶即可，不必等腳本結束。

腳本流程：
1. 從 `~/.hermes/.env` 讀取憑證（`HCL_PORTAL_URL` / `HCL_VERSE_URL` / `HCL_USERNAME` / `HCL_PASSWORD`）
2. 開啟 Chromium（有頭模式）登入 Portal
3. 導航到 Verse 收件匣，等待信件列表載入
4. 保持瀏覽器開啟，用戶關閉視窗後腳本結束

## 結果呈現

成功時告知用戶：

```
✓ 已登入 HCL Verse，瀏覽器已開啟收件匣，可直接操作。
```

失敗時列出錯誤訊息（常見：網路連線、密碼過期、Portal 改版導致 selector 失效）。

## 常見問題

- **登入欄位找不到**：Portal 頁面改版，需更新 `page.fill` 的 selector
- **treeitem 等待超時**：Verse 載入慢或網路問題，重新執行即可

## Changelog

- 1.0.0 (2026-06-03): 初始版本 — 登入並開啟 Verse 收件匣，保持瀏覽器開啟
