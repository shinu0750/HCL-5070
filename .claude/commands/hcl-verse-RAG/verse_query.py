#!/usr/bin/env python3
"""
HCL Verse 查詢腳本
==================
三種模式，自動判斷或手動指定：

  --reflect   "問題"         → Hindsight reflect（問答/追蹤）
  --model     "專案名"       → 讀取 proj mental model（進度摘要）
  --search    "關鍵字"       → Qdrant 向量搜尋（找信）
  --all       "問題"         → 三種全跑（預設）

用法：
  python3 verse_query.py "PharmaSuite 最新進度"
  python3 verse_query.py --model "PharmaSuite/MES"
  python3 verse_query.py --search "8D 報告" --top 5
  python3 verse_query.py --reflect "V3F 洩漏事件後來怎樣了"
"""
import os, sys, json, argparse, requests, tempfile
from pathlib import Path

_env = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env):
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

from qdrant_client import QdrantClient
from qdrant_client.models import NamedVector
from openai import OpenAI

sys.path.insert(0, os.path.expanduser("~/Claude/HCL"))
from project_keywords import PROJECT_KEYWORDS, match_projects

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888/mcp/")
QDRANT_URL    = os.environ.get("QDRANT_URL",    "http://10.11.1.40:6333")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "http://localhost:8081/v1")
EMBEDDING_MODEL    = os.environ.get("EMBEDDING_MODEL",    "jina-embed")
COLLECTION    = "verse_emails"
OUTPUT_FILE   = os.path.join(tempfile.gettempdir(), "verse_query_result.json")

# 專案名稱 → mental model id 的查表（啟動時從 Hindsight 抓）
_model_map: dict[str, str] = {}


# ── Hindsight HTTP client ─────────────────────────────────────────────────────
class HindsightClient:
    def __init__(self, url):
        self.url = url
        resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "verse-query", "version": "1.0"}},
        })
        self.session_id = resp.headers.get("mcp-session-id")
        if not self.session_id:
            raise RuntimeError(f"Hindsight 初始化失敗：{resp.text[:200]}")

    def _call(self, name, arguments):
        resp = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, headers={"mcp-session-id": self.session_id}, timeout=60)
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise RuntimeError(f"無法解析：{resp.text[:200]}")

    def reflect(self, query):
        return self._call("reflect", {"query": query})

    def list_mental_models(self):
        return self._call("list_mental_models", {})

    def get_mental_model(self, model_id):
        return self._call("get_mental_model", {"mental_model_id": model_id})


def build_model_map(hindsight: HindsightClient):
    """從 Hindsight 拉出所有 mental model，建立「專案名 → model_id」對照表。"""
    resp = hindsight.list_mental_models()
    models = resp.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        data = json.loads(models) if isinstance(models, str) else models
    except Exception:
        data = []
    if isinstance(data, dict):
        data = data.get("mental_models", [])
    for m in (data or []):
        name = m.get("name", "")
        mid  = m.get("id") or m.get("mental_model_id", "")
        # 名稱格式：「PharmaSuite/MES 專案進度」→ 取前半部分
        proj_name = name.replace(" 專案進度", "").replace(" 進度", "").strip()
        if mid:
            _model_map[proj_name] = mid
            _model_map[name]      = mid  # 也存全名


def find_model_id(query: str) -> str | None:
    """從查詢文字猜出最相關的 mental model id。"""
    # 完全匹配
    if query in _model_map:
        return _model_map[query]
    # 部分匹配
    for key, mid in _model_map.items():
        if query in key or key in query:
            return mid
    # 用 match_projects 找專案再查
    projs = match_projects(query)
    for proj in projs:
        if proj in _model_map:
            return _model_map[proj]
    return None


# ── 三種查詢模式 ──────────────────────────────────────────────────────────────
def do_reflect(hindsight: HindsightClient, query: str) -> dict:
    print(f"[reflect] {query}")
    resp = hindsight.reflect(query)
    content = resp.get("result", {}).get("content", [{}])
    text = content[0].get("text", "") if content else str(resp)
    return {"mode": "reflect", "query": query, "answer": text}


def do_model(hindsight: HindsightClient, query: str) -> dict:
    mid = find_model_id(query)
    if not mid:
        return {"mode": "model", "query": query, "error": f"找不到對應的 mental model（已知：{list(_model_map.keys())[:5]}...）"}
    print(f"[model] {query} → {mid}")
    resp = hindsight.get_mental_model(mid)
    content = resp.get("result", {}).get("content", [{}])
    text = content[0].get("text", "") if content else str(resp)
    return {"mode": "model", "query": query, "model_id": mid, "content": text}


def do_search(query: str, top_k: int = 5) -> dict:
    print(f"[search] {query} (top {top_k})")
    qdrant = QdrantClient(url=QDRANT_URL, timeout=30)
    cli    = OpenAI(api_key=OPENAI_KEY or "local-no-key-needed", base_url=EMBEDDING_API_BASE)

    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        return {"mode": "search", "query": query, "error": f"Collection '{COLLECTION}' 不存在"}

    emb = cli.embeddings.create(model=EMBEDDING_MODEL, input=query).data[0].embedding
    hits = qdrant.query_points(collection_name=COLLECTION, query=emb,
                               limit=top_k, with_payload=True).points

    # 討論串折疊：同 thread_id 只留最高分
    seen_threads: dict[str, dict] = {}
    results = []
    for hit in hits:
        p = hit.payload or {}
        tid = p.get("thread_id", hit.id)
        entry = {
            "score":      round(hit.score, 4),
            "subject":    p.get("subject", ""),
            "from":       p.get("from", ""),
            "date":       p.get("date", ""),
            "snippet":    (p.get("body") or "")[:300],
            "gmail_id":   p.get("gmail_id", ""),
            "eml_path":   p.get("eml_path", ""),
            "thread_id":  tid,
        }
        if tid not in seen_threads or hit.score > seen_threads[tid]["score"]:
            seen_threads[tid] = entry
    results = sorted(seen_threads.values(), key=lambda x: x["score"], reverse=True)
    return {"mode": "search", "query": query, "hits": results}


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query",          nargs="?", default="",   help="查詢文字")
    ap.add_argument("--reflect",      metavar="Q",             help="問答模式（Hindsight reflect）")
    ap.add_argument("--model",        metavar="PROJ",          help="進度摘要（mental model）")
    ap.add_argument("--search",       metavar="Q",             help="找信模式（Qdrant 向量搜尋）")
    ap.add_argument("--top",          type=int, default=5,     help="搜尋結果數（預設 5）")
    ap.add_argument("--all",          action="store_true",     help="三種全跑")
    args = ap.parse_args()

    query = args.query or ""
    hindsight = HindsightClient(HINDSIGHT_URL)
    build_model_map(hindsight)

    results = []

    if args.reflect:
        results.append(do_reflect(hindsight, args.reflect))
    elif args.model:
        results.append(do_model(hindsight, args.model))
    elif args.search:
        results.append(do_search(args.search, args.top))
    elif query or args.all:
        q = query or args.all
        # 自動判斷：有 proj 命中且問的是進度 → model；否則 reflect + search 並列
        projs = match_projects(q) if isinstance(q, str) else []
        progress_words = ["進度", "狀態", "最新", "目前", "怎麼了", "如何", "summary"]
        is_progress = any(w in q for w in progress_words) if isinstance(q, str) else False

        if projs and is_progress:
            results.append(do_model(hindsight, q))
        results.append(do_reflect(hindsight, q if isinstance(q, str) else query))
        results.append(do_search(q if isinstance(q, str) else query, args.top))
    else:
        ap.print_help()
        sys.exit(0)

    output = {"results": results}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 印出給 Claude 讀
    for r in results:
        mode = r.get("mode", "")
        print(f"\n{'='*60}")
        print(f"[{mode.upper()}]")
        if mode == "reflect":
            print(r.get("answer", r.get("error", "")))
        elif mode == "model":
            print(r.get("content", r.get("error", "")))
        elif mode == "search":
            for h in r.get("hits", []):
                print(f"  {h['score']:.3f} | {h['date'][:10]} | {h['subject'][:50]}")
                print(f"         from: {h['from']}")
                if h.get("snippet"):
                    print(f"         {h['snippet'][:150].replace(chr(10), ' ')}")
        if r.get("error"):
            print(f"  ⚠ {r['error']}")

    print(f"\n結果已寫入 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
