將 Gmail thread(s) 以**每個 thread 為單位**各自獨立寫入 Hindsight，讓 Hindsight 自行建立語意連結。
同一 thread 內的多封信合併為一筆；不同 thread 各自獨立。

## 使用方式

```
/gmail-to-hindsight-OneByOne <thread_id1> [thread_id2] ...
```

## 與 gmail-to-hindsight-ALL 的差異

| | gmail-to-hindsight-ALL | gmail-to-hindsight-OneByOne |
|---|---|---|
| 寫入單位 | 所有 thread 合成 1 筆 | 每個 thread = 1 筆 |
| document_id | `email-thread-{subject}` | `email-thread-{thread_id}` |
| 跨 thread 關聯 | 手動合併在同一筆 | 交給 Hindsight 語意連結 |
| 更新方式 | 新信進來覆蓋整串 | 重跑同 thread 直接覆蓋更新 |

## 執行步驟

### Step 1：抓取所有 thread 資料

對每個傳入的 thread ID 呼叫 Gmail MCP connector（`mcp__81301edd-d843-4be2-a702-36c15f247565__get_thread`）。

若單一 thread 資料量過大，存到暫存檔後從 WSL 路徑讀取：
- `\\wsl.localhost\Ubuntu\home\eid\scripts\email-to-hindsight\tmp_thread_{id}.json`

### Step 2：存成 input JSON

將所有 thread 資料（list of thread objects）寫到：
```
\\wsl.localhost\Ubuntu\home\eid\scripts\email-to-hindsight\input_threads.json
```

### Step 3：執行腳本

```bash
docker exec python-tools python3 /scripts/email-to-hindsight/process_one_by_one.py \
  --input-json /scripts/email-to-hindsight/input_threads.json
```

腳本自動：
- 每個 thread 各自處理，document_id = `email-thread-{thread_id}`
- 同一 thread 內多封信合併為一筆
- LLM 分類以整串為單位（呼叫 Ollama gemma4:e4b，容器內 `http://ollama:11434`）
- 相同 document_id 直接覆蓋（支援新信進來重跑更新）

### Step 4：回報結果

顯示：
- 共幾個 thread
- 每個 thread 的 document_id、信件數、category、寫入狀態

## 常數

| 項目 | 值 |
|------|-----|
| Hindsight base | `http://localhost:8888` |
| Hindsight bank | `shuhsing` |
| Gmail MCP | `mcp__81301edd-d843-4be2-a702-36c15f247565__get_thread` |
| 腳本路徑（容器內） | `/scripts/email-to-hindsight/process_one_by_one.py` |
| 輸入 JSON（WSL） | `~/scripts/email-to-hindsight/input_threads.json` |
| 輸入 JSON（容器內） | `/scripts/email-to-hindsight/input_threads.json` |
