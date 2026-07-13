#!/usr/bin/env python3
"""
會議記錄 / 報價單附件 -> RAGAnything（共用知識庫）+ Hindsight（僅會議記錄）

只在 verse_archive_pipeline.py 歸檔「以後新進來」的信時觸發（不 backfill 舊信）。
判定依據：附件檔名 or 信件主旨，符合關鍵字就當作候選（見 classify_attachment()）。
只處理 .pdf 附件——RAGAnything/MinerU 理論上能解析 docx/pptx，但目前只在 PDF 上
實測過，其他格式先跳過，之後有需要再擴充。

**分兩階段，不在歸檔當下同步解析**（3.15.0 改版）：`upload_to_raganything()`
（`docker compose exec` 跑 MinerU 版面解析 + LLM 圖表說明）單一附件可能要跑好幾分鐘，
`PROCESS_TIMEOUT_SEC=1800` 甚至給到 30 分鐘容錯——如果在 verse_archive_pipeline.py
的歸檔迴圈裡同步呼叫，整支 pipeline 會被這一個附件卡住這麼久，其他信件都得等。
改成：
  1. **歸檔當下**（`process_meeting_quote_attachments()`）：只把符合關鍵字的 .pdf
     附件另存一份到 `MEETING_QUOTE_STAGING_DIR`（部門共用網路磁碟），旁邊多存一個
     同名 `.json` sidecar 記 unid/subject/sender_name/sent_date/labels，不呼叫
     RAGAnything，不寫 Hindsight，幾乎不花時間，不拖慢歸檔本身
  2. **歸檔全部跑完後另外執行**（`meeting_quote_batch_process.py`）：掃描
     `MEETING_QUOTE_STAGING_DIR`，逐一讀 sidecar 取回 metadata → 送進 RAGAnything
     解析 → 「會議記錄」類額外把解析出的全文寫進 Hindsight → 成功的搬到
     `done/` 子目錄（沿用 EML 上傳 Gmail 的 done 慣例，失敗的留原地方便重跑）
"""
import os
import re
import json
import hashlib
import subprocess

# WSL 那邊的路徑設定見 C:\Users\EID\Documents\Claude\ShuHsing\WSL\CLAUDE.md
WSL_ROOT = r"C:\Users\EID\Documents\Claude\ShuHsing\WSL"
RAGANYTHING_INPUTS_DIR = os.path.join(WSL_ROOT, "inputs")
RAGANYTHING_OUTPUT_DIR = os.path.join(WSL_ROOT, "output")
DOCKER_COMPOSE_FILE = "/mnt/c/Users/EID/Documents/Claude/ShuHsing/WSL/docker-compose.yml"

# 分兩階段設計下的「暫存區」：歸檔當下只存檔到這裡，事後 meeting_quote_batch_process.py
# 才真的送進 RAGAnything。部門共用網路磁碟，可用同名環境變數覆寫（跟 EML_OUTPUT_DIR
# 同樣的慣例）
MEETING_QUOTE_STAGING_DIR = os.environ.get(
    "MEETING_QUOTE_STAGING_DIR",
    r"\\10.11.1.40\工程管理暨智慧製造處\公用區-Hermes\meeting minutes")

# 解析一份文件（尤其是圖表多的會議記錄）可能要跑好幾分鐘（MinerU 版面解析 + LLM
# 圖片/表格說明），給寬鬆的 timeout，避免長文件被誤判成掛掉
PROCESS_TIMEOUT_SEC = 1800

MEETING_KEYWORDS = ["會議記錄", "會議紀錄", "會議紀要", "meeting minutes", "minutes of meeting"]
QUOTE_KEYWORDS = ["報價單", "報價", "quotation", "quote"]


def classify_attachment(filename, subject):
    """回傳這個附件符合的類別集合：{"meeting"} / {"quote"} / 兩者皆有 / 空集合。
    檔名或主旨任一邊符合關鍵字就算數（信件主旨當輔助訊號，擴大命中率）。"""
    haystack = f"{filename or ''} {subject or ''}".lower()
    labels = set()
    if any(kw.lower() in haystack for kw in MEETING_KEYWORDS):
        labels.add("meeting")
    if any(kw.lower() in haystack for kw in QUOTE_KEYWORDS):
        labels.add("quote")
    return labels


def _safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name or '').strip().strip('.')
    return (cleaned or "attachment")[:150]


def _stem(filename):
    return os.path.splitext(filename)[0]


def save_for_batch_processing(unid, name, data, subject, sender_name, sent_date, labels):
    """歸檔當下呼叫：只存檔，不觸發 RAGAnything/Hindsight（見檔案開頭兩階段說明）。
    PDF 跟同名 .json sidecar 一起存到 MEETING_QUOTE_STAGING_DIR，sidecar 記
    meeting_quote_batch_process.py 事後處理需要的 metadata（RAGAnything 只認檔案
    本身，不會保留這些資訊，一定要另外存）。檔名沿用 unid 前綴慣例，避免同名衝突、
    也方便回頭比對 Qdrant/Hindsight 裡的同一筆資料。"""
    os.makedirs(MEETING_QUOTE_STAGING_DIR, exist_ok=True)
    fname = f"{unid}_{_safe_filename(name)}"
    pdf_path = os.path.join(MEETING_QUOTE_STAGING_DIR, fname)
    with open(pdf_path, 'wb') as f:
        f.write(data)
    json_path = os.path.join(MEETING_QUOTE_STAGING_DIR, _stem(fname) + ".json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            "unid": unid, "original_name": name, "subject": subject,
            "sender_name": sender_name, "sent_date": sent_date,
            "labels": sorted(labels),
        }, f, ensure_ascii=False, indent=2)
    return pdf_path


def save_to_inputs(unid, name, data):
    """把附件 bytes 存進 RAGAnything 的 inputs 目錄，回傳 (磁碟路徑, docker 裡看到的檔名)。"""
    os.makedirs(RAGANYTHING_INPUTS_DIR, exist_ok=True)
    fname = f"{unid}_{_safe_filename(name)}"
    path = os.path.join(RAGANYTHING_INPUTS_DIR, fname)
    with open(path, 'wb') as f:
        f.write(data)
    return path, fname


def upload_to_raganything(fname):
    """觸發 docker compose exec 處理 inputs 目錄裡的這個檔案，併入共用知識庫。
    回傳 (成功與否, stdout/stderr 摘要)。"""
    try:
        result = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "docker", "compose", "-f", DOCKER_COMPOSE_FILE,
             "exec", "-T", "raganything", "python3", "/app/scripts/process_pdf.py", fname],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROCESS_TIMEOUT_SEC,
        )
        ok = result.returncode == 0
        detail = (result.stdout or "")[-2000:] + (result.stderr or "")[-2000:]
        return ok, detail
    except subprocess.TimeoutExpired:
        return False, f"process_pdf.py 逾時（>{PROCESS_TIMEOUT_SEC}s）"
    except Exception as e:
        return False, str(e)


def _long_path(p):
    """Windows MAX_PATH（260 字元）保護：output 目錄結構會把同一個檔名 stem
    重複三層（{stem}_{hash}/{stem}/hybrid_auto/{stem}.md），會議記錄主旨本來
    就長，疊三層很容易破 260 字元上限，屆時 os.path.isfile()/glob 會直接靜默
    回傳 False/[]（不會噴例外，很容易誤判成「找不到檔案」）。前面加
    \\\\?\\ 這個 Windows 專用的 extended-length prefix 可以繞過限制，已用真實
    案例驗證過（len=283 的路徑，不加 prefix 完全讀不到，加了就正常）。"""
    ap = os.path.abspath(p)
    return ap if ap.startswith("\\\\?\\") else "\\\\?\\" + ap


def find_parsed_markdown(fname):
    """process_pdf.py 完成後，解析結果在
    output/{stem}_{8碼hash}/{stem}/hybrid_auto/{stem}.md，hash 每次不可預期，
    用 os.listdir() 逐層找（不用 glob——它底層一樣會受 MAX_PATH 影響），
    同名有多筆就取最新修改時間那筆。找不到回傳 None。"""
    stem = _stem(fname)
    out_dir = _long_path(RAGANYTHING_OUTPUT_DIR)
    if not os.path.isdir(out_dir):
        return None

    candidates = []
    prefix = stem + "_"
    for entry in os.listdir(out_dir):
        if not entry.startswith(prefix):
            continue
        md_path = _long_path(os.path.join(RAGANYTHING_OUTPUT_DIR, entry, stem, "hybrid_auto", stem + ".md"))
        if os.path.isfile(md_path):
            candidates.append(md_path)

    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    with open(candidates[0], encoding="utf-8") as f:
        return f.read()


def write_meeting_to_hindsight(hindsight, unid, attachment_name, markdown_text,
                                subject, sender_name, sent_date):
    """把會議記錄全文寫進 Hindsight（EID bank），跟訊息本文（tags=["mail"]）分開，
    多一個 "meeting-minutes" tag，方便 reflect()/recall() 專門查會議記錄全文。
    document_id 用 unid+檔名 hash 組，避免跟訊息本體的 document_id（純 unid）撞到，
    同一則訊息如果有多個會議記錄附件也不會互相覆蓋。"""
    doc_suffix = hashlib.md5(attachment_name.encode("utf-8")).hexdigest()[:8]
    document_id = f"{unid}_meeting_{doc_suffix}"
    content = (
        f"會議記錄全文（附件：{attachment_name}）\n"
        f"主旨：{subject}\n寄件者：{sender_name}\n日期：{sent_date}\n\n{markdown_text}"
    )
    metadata = {
        "subject": subject, "from_name": sender_name, "sent_date": sent_date,
        "unid": unid, "attachment_name": attachment_name,
        "doc_type": "meeting-minutes",
    }
    return hindsight.retain(
        content=content, document_id=document_id, timestamp=sent_date,
        metadata=metadata, tags=["mail", "meeting-minutes"],
        context=f"會議記錄全文：主旨「{subject}」，附件「{attachment_name}」",
    )


def process_meeting_quote_attachments(unid, subject, sender_name, sent_date, attachments_data):
    """attachments_data: [(name, bytes), ...]（來自 download_attachments()）。
    只挑 .pdf、且符合會議記錄/報價單關鍵字的附件，另存到 MEETING_QUOTE_STAGING_DIR
    （見檔案開頭兩階段說明），不觸發 RAGAnything/Hindsight——那是
    meeting_quote_batch_process.py 事後才做的事，這裡只要快、不能拖慢歸檔本身。
    回傳每個候選附件的存檔紀錄，供呼叫端 print/記錄用，不影響信件本身的
    RAG/Hindsight/EML/搬移流程——這一段失敗只印警告，不拋例外中斷主流程。"""
    records = []
    for name, data in attachments_data or []:
        if not name.lower().endswith(".pdf"):
            continue
        labels = classify_attachment(name, subject)
        if not labels:
            continue

        rec = {"name": name, "labels": sorted(labels), "saved": False}
        try:
            save_for_batch_processing(unid, name, data, subject, sender_name, sent_date, labels)
            rec["saved"] = True
        except Exception as e:
            rec["error"] = str(e)

        records.append(rec)
    return records
