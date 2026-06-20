#!/usr/bin/env python3
"""
HCL Verse RAG 語意搜尋腳本
用法：python3 verse_rag_search.py "查詢文字" [top_k]
"""
import os, sys, json

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

QDRANT_URL  = os.environ.get("QDRANT_URL",     "http://localhost:6333")
OPENAI_KEY  = os.environ.get("OPENAI_API_KEY", "")
COLLECTION  = "verse_emails"
OUTPUT_FILE = "/tmp/verse_rag_search_result.json"

qdrant        = QdrantClient(url=QDRANT_URL)
openai_client = OpenAI(api_key=OPENAI_KEY)


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
    res = openai_client.embeddings.create(model="text-embedding-3-small", input=query)
    embedding = res.data[0].embedding

    # 搜尋 Qdrant
    hits = qdrant.search(
        collection_name=COLLECTION,
        query_vector=embedding,
        limit=k,
        with_payload=True,
    )

    results = []
    for hit in hits:
        payload = hit.payload or {}
        results.append({
            "score":   round(hit.score, 4),
            "subject": payload.get("subject", ""),
            "from":    payload.get("from", ""),
            "date":    payload.get("date", ""),
            "snippet": payload.get("snippet", ""),
        })

    output = {"query": query, "results": results}
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, ensure_ascii=False)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
