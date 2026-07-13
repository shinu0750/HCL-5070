#!/usr/bin/env python3
"""
HCL Verse RAG 語意搜尋腳本
用法：python3 verse_rag_search.py "查詢文字" [top_k]
"""
import os, sys, json, tempfile

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

from qdrant_client import QdrantClient
from openai import OpenAI

QDRANT_URL  = os.environ.get("QDRANT_URL",     "http://10.11.1.40:6333")
OPENAI_KEY  = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "http://localhost:8081/v1")
EMBEDDING_MODEL    = os.environ.get("EMBEDDING_MODEL",    "jina-embed")
COLLECTION  = "verse_emails"
OUTPUT_FILE = os.path.join(tempfile.gettempdir(), "verse_rag_search_result.json")

qdrant        = QdrantClient(url=QDRANT_URL, timeout=30)
openai_client = OpenAI(api_key=OPENAI_KEY or "local-no-key-needed", base_url=EMBEDDING_API_BASE)


def main():
    if len(sys.argv) < 2:
        print("用法：python3 verse_rag_search.py '查詢文字' [top_k]")
        sys.exit(1)

    query = sys.argv[1]
    k     = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    # 確認 collection 存在
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        result = {"error": f"Collection '{COLLECTION}' 不存在，請先執行 verse_rag_index.py 建立索引。"}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    # 產生 query embedding
    res = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=query)
    embedding = res.data[0].embedding

    # 搜尋 Qdrant（qdrant-client 新版移除了 .search()，改用 .query_points()）
    hits = qdrant.query_points(
        collection_name=COLLECTION,
        query=embedding,
        limit=k,
        with_payload=True,
    ).points

    results = []
    for hit in hits:
        payload = hit.payload or {}
        results.append({
            "score":      round(hit.score, 4),
            "subject":    payload.get("subject", ""),
            "from_name":  payload.get("from_name", ""),
            "from_email": payload.get("from_email", ""),
            "sent_date":  payload.get("sent_date") or payload.get("date", ""),
            "snippet":    payload.get("body", "")[:150],
            "unid":       payload.get("unid", ""),
            "reply_to_unid": payload.get("reply_to_unid"),
        })

    output = {"query": query, "results": results}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
