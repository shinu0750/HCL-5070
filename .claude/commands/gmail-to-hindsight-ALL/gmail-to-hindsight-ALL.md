將 Gmail thread(s) 疊加寫入 Hindsight，自動合併同主旨的歷史 thread ID。

## 使用方式

```
/gmail-to-hindsight-ALL <thread_id1> [thread_id2] ...
```

## 執行步驟

### Step 1：抓取新 thread 取得主旨

對每個傳入的 thread ID 呼叫 Gmail MCP connector（`mcp__81301edd-d843-4be2-a702-36c15f247565__get_thread`）。

取出第一封信的 subject，去掉 Re:/回覆: 前綴，計算 document_id：
```
document_id = "email-thread-" + subject（特殊字元換成 -，最多60字）
```

### Step 2：查 Hindsight 是否已有此 document

呼叫：
```
GET http://localhost:8888/v1/default/banks/shuhsing/documents/{document_id}
```
（從 WSL 或 Windows 都用 localhost:8888）

- **若存在**：從回傳的 `document_metadata.thread_ids` 取出舊的 thread ID list，與本次傳入的 ID 合併（去重）→ 得到完整 thread ID list
- **若不存在（404）**：只用本次傳入的 ID

### Step 3：抓取所有 thread 資料

對 Step 2 得到的**完整 thread ID list** 逐一呼叫 Gmail MCP `get_thread`。

若單一 thread 資料量過大（超過 context 限制），將該 thread 存到暫存檔後從 WSL 路徑讀取：
- 暫存路徑：`\\wsl.localhost\Ubuntu\home\eid\scripts\email-to-hindsight\tmp_thread_{id}.json`

### Step 4：組合 JSON 並存檔

將所有 thread 合併成 JSON array，寫到：
```
\\wsl.localhost\Ubuntu\home\eid\scripts\email-to-hindsight\input_threads.json
```

### Step 5：執行腳本

```bash
docker exec python-tools python3 /scripts/email-to-hindsight/process_thread.py \
  --input-json /scripts/email-to-hindsight/input_threads.json
```

### Step 6：回報結果

顯示：
- 主旨
- 本次新增 / 已存在的 thread ID
- 最終合併後共幾個 thread、幾封信
- LLM 分類（category、tags）
- 寫入狀態

## 常數

| 項目 | 值 |
|------|-----|
| Hindsight base | `http://localhost:8888` |
| Hindsight bank | `shuhsing` |
| Gmail MCP | `mcp__81301edd-d843-4be2-a702-36c15f247565__get_thread` |
| 腳本路徑（容器內） | `/scripts/email-to-hindsight/process_thread.py` |
| 輸入 JSON（容器內） | `/scripts/email-to-hindsight/input_threads.json` |
