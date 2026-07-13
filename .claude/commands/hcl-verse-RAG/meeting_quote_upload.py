#!/usr/bin/env python3
"""
會議記錄 / 報價單附件 -> RAGAnything（共用知識庫）+ Hindsight（僅會議記錄）

只在 verse_archive_pipeline.py 歸檔「以後新進來」的信時觸發（不 backfill 舊信）。
判定依據：附件檔名 or 信件主旨，符合關鍵字就當作候選（見 classify_attachment()）。
只處理 .pdf 附件——RAGAnything/MinerU 理論上能解析 docx/pptx，但目前只在 PDF 上
實測過，其他格式先跳過，之後有需要再擴充。

流程：
  1. 附件 bytes 寫進 RAGAnything 的 inputs 目錄（檔名前綴 unid 避免同名衝突，
     跟 verse_archive_pipeline.py 的 ATTACHMENTS_DIR 命名慣例一致）
  2. `docker compose exec raganything python3 /app/scripts/process_pdf.py <檔名>`
     解析並併入共用知識庫（不分 project/workspace，機密內容也丟——已跟使用者
     確認過可以接受，見對話記錄）
  3. 只有「會議記錄」類的附件，額外去 RAGAnything 的 output 目錄撈解析出來的
     markdown 全文，寫進 Hindsight（EID bank），讓 reflect()/recall() 之後
     可以直接查到會議記錄全文，不用每次都重新開會議記錄 PDF
"""
import os
import re
import hashlib
import subprocess

# WSL 那邊的路徑設定見 C:\Users\EID\Documents\Claude\ShuHsing\WSL\CLAUDE.md
WSL_ROOT = r"C:\Users\EID\Documents\Claude\ShuHsing\WSL"
RAGANYTHING_INPUTS_DIR = os.path.join(WSL_ROOT, "inputs")
RAGANYTHING_OUTPUT_DIR = os.path.join(WSL_ROOT, "output")
DOCKER_COMPOSE_FILE = "/mnt/c/Users/EID/Documents/Claude/ShuHsing/WSL/docker-compose.yml"

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


def process_meeting_quote_attachments(hindsight, unid, subject, sender_name, sent_date,
                                       attachments_data):
    """attachments_data: [(name, bytes), ...]（來自 download_attachments()）。
    只挑 .pdf、且符合會議記錄/報價單關鍵字的附件處理，其餘原樣跳過。
    回傳每個候選附件的處理紀錄，供呼叫端 print/記錄用，不影響信件本身的
    RAG/Hindsight/EML/搬移流程——這一段失敗只印警告，不拋例外中斷主流程。"""
    records = []
    for name, data in attachments_data or []:
        if not name.lower().endswith(".pdf"):
            continue
        labels = classify_attachment(name, subject)
        if not labels:
            continue

        rec = {"name": name, "labels": sorted(labels), "raganything_ok": False, "hindsight_ok": None}
        try:
            _, fname = save_to_inputs(unid, name, data)
            ok, detail = upload_to_raganything(fname)
            rec["raganything_ok"] = ok
            if not ok:
                rec["error"] = detail[:500]
        except Exception as e:
            rec["error"] = str(e)
            records.append(rec)
            continue

        if "meeting" in labels and rec["raganything_ok"]:
            try:
                md_text = find_parsed_markdown(fname)
                if md_text:
                    result = write_meeting_to_hindsight(
                        hindsight, unid, name, md_text, subject, sender_name, sent_date)
                    result_text = result.get("result", {}).get("content", [{}])[0].get("text", "")
                    rec["hindsight_ok"] = "validation error" not in result_text.lower()
                else:
                    rec["hindsight_ok"] = False
                    rec["error"] = "找不到解析後的 markdown（output 目錄沒對應檔案）"
            except Exception as e:
                rec["hindsight_ok"] = False
                rec["error"] = str(e)

        records.append(rec)
    return records
