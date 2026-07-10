#!/usr/bin/env python3
"""
獨立模組：查詢公司通訊錄（PostgreSQL email_mapping 表），
提供 email<->姓名 雙向查詢，還有「me」解析（目前登入帳號 -> email/姓名）。

之後要整合進 verse_archive_pipeline.py 的 header 處理（to/cc/from 統一存 email，
Hindsight content 用姓名），先獨立寫、獨立測。
"""
import os
import psycopg2

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB   = os.environ.get("PG_DB", "")
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")

EMAIL_DOMAIN = "ecic.com.tw"  # 目前觀察到的內部帳號 email 規則：{HCL_USERNAME}@ecic.com.tw

_cache_email_to_name = None
_cache_name_to_email = None


def get_connection():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
    )


def _load_cache():
    global _cache_email_to_name, _cache_name_to_email
    if _cache_email_to_name is not None:
        return
    _cache_email_to_name = {}
    _cache_name_to_email = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, email FROM email_mapping")
            for name, email in cur.fetchall():
                if email:
                    _cache_email_to_name[email.strip().lower()] = name
                if name:
                    _cache_name_to_email.setdefault(name.strip(), email)
    finally:
        conn.close()


def email_to_name(email):
    """email -> 姓名，查不到回傳原本的 email"""
    if not email:
        return email
    _load_cache()
    return _cache_email_to_name.get(email.strip().lower(), email)


def name_to_email(name):
    """姓名 -> email，查不到回傳 None"""
    if not name:
        return None
    _load_cache()
    return _cache_name_to_email.get(name.strip())


def resolve_me(hcl_username):
    """目前登入帳號（HCL_USERNAME）-> (email, 姓名)"""
    email = f"{hcl_username}@{EMAIL_DOMAIN}"
    name = email_to_name(email)
    return email, name


def _run_self_test():
    print(f"連線設定: host={PG_HOST} port={PG_PORT} db={PG_DB} user={PG_USER}")
    _load_cache()
    print(f"共讀到 {len(_cache_email_to_name)} 筆 email->name 對照")

    samples = ["ycmu@ecic.com.tw", "shuhsing@ecic.com.tw", "tzuyu@ecic.com.tw"]
    for e in samples:
        print(f"  {e} -> {email_to_name(e)}")

    print("resolve_me('shuhsing') ->", resolve_me("shuhsing"))
    print("resolve_me('ycmu') ->", resolve_me("ycmu"))


if __name__ == "__main__":
    _run_self_test()
