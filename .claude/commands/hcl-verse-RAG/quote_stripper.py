#!/usr/bin/env python3
"""
獨立模組：偵測 email 內文裡的「引用歷史」分隔符，砍掉分隔符之後的內容。
規則來自對 04Done 18 個真實討論串的統計（寄件人:/发件人:/From:+Sent:/
-----Original Message-----/-----邮件原件-----/Notes 內嵌 "名字" ---日期---）。

之後要整合進 verse_archive_pipeline.py 的 clean_body()，先獨立寫、獨立測。
"""
import re

# 依統計出現頻率排序（不影響功能，只是方便閱讀）：
#   寄件人:/发件人:  443+2 次、17/18 檔案（含 收件者:/副本抄送: 同一表頭區塊，不用重複偵測）
#   From:+Sent:       171 次、6/18 檔案（外部廠商用 Outlook 時出現）
#   -----Original Message-----  150 次、4/18 檔案
#   Notes 內嵌 "名字" ---日期---  27 次、5/18 檔案
#   -----郵件原件/邮件原件-----   2 次、1/18 檔案
QUOTE_BOUNDARY_PATTERNS = [
    re.compile(r'寄件人[:：]'),
    re.compile(r'寄件者[:：]'),  # HCL Verse 手機版用「寄件者」，跟桌面版「寄件人」同義不同字
    re.compile(r'发件人[:：]'),
    re.compile(r'From:\s*\S.*?\n\s*Sent:', re.DOTALL),
    re.compile(r'-{3,}\s*Original Message\s*-{3,}', re.IGNORECASE),
    re.compile(r'-{3,}\s*[邮郵]件原件\s*-{3,}'),
    re.compile(r'"?[^"\n]{1,30}?"?\s*---\s*\d{4}[/年]\d{1,2}[/月]\d{1,2}[^\n-]{0,25}---'),
    # Notes 內嵌：「"名字" ---2026/07/08...---」或「名字---2026/07/08...---」（引號可有可無）
]


def find_quote_boundary(text):
    """回傳最早出現的引用分隔符起始位置；都沒找到回 None。"""
    earliest = None
    for pat in QUOTE_BOUNDARY_PATTERNS:
        m = pat.search(text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    return earliest


def strip_quoted_history(text):
    """砍掉第一個引用分隔符之後的內容，只留上面新增的部分。"""
    idx = find_quote_boundary(text)
    if idx is None:
        return text.strip()
    return text[:idx].rstrip()


# ── 被引用者身份抽取：砍之前先看被砍掉那段開頭是誰、什麼時候 ────────────────
# Notes 內嵌引用：「"名字" ---日期---」或「名字---日期---」（引號可有可無）
_IDENTITY_INLINE = re.compile(
    r'"?([^"\n]{1,30}?)"?\s*---\s*(\d{4}[/年]\d{1,2}[/月]\d{1,2}[^\n-]{0,25})---'
)
# 中文表頭區塊：寄件人:/寄件者:/发件人: 後面（同一行）接姓名或 email
_IDENTITY_HEADER_SENDER = re.compile(r'(?:寄件人|寄件者|发件人)[:：]\s*"?([^"<\[\n]{1,50}?)(?:\s*[\[<"]|\s*$|\n)')
_IDENTITY_HEADER_DATE = re.compile(r'(?:日期|發送時間|发送时间)[:：]\s*([^\n]{1,40})')
# 英文 Outlook 表頭區塊：From:/Sent:
_IDENTITY_FROM = re.compile(r'From:\s*([^\n<]{1,60})')
_IDENTITY_SENT = re.compile(r'Sent:\s*([^\n]{1,60})')


def extract_quoted_identity(text, boundary_idx, window=300):
    """從引用分隔符開始往後一小段文字，抽取「被引用的是誰、什麼時候」。
    回傳 (sender, date_str)，抽不到的欄位回傳 None。"""
    if boundary_idx is None:
        return None, None
    snippet = text[boundary_idx:boundary_idx + window]

    m = _IDENTITY_INLINE.match(snippet)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    sender = date_str = None
    m = _IDENTITY_HEADER_SENDER.search(snippet)
    if m:
        sender = m.group(1).strip().rstrip('"')
    m = _IDENTITY_HEADER_DATE.search(snippet)
    if m:
        date_str = m.group(1).strip()

    if sender is None:
        m = _IDENTITY_FROM.search(snippet)
        if m:
            sender = m.group(1).strip()
        m = _IDENTITY_SENT.search(snippet)
        if m:
            date_str = m.group(1).strip()

    return sender, date_str


def strip_quoted_history_with_identity(text):
    """砍掉引用歷史，同時回傳被砍掉那段的身份資訊，方便之後拿去跟同一個討論串裡
    其他訊息比對，建立「這則是回覆哪一則」的關聯（reply_to）。
    回傳 (cleaned_text, quoted_sender, quoted_date)。"""
    idx = find_quote_boundary(text)
    if idx is None:
        return text.strip(), None, None
    sender, date_str = extract_quoted_identity(text, idx)
    return text[:idx].rstrip(), sender, date_str


# ── 自我測試：拿之前手動確認過的兩個真實訊息當基準 ──────────────────────────
_SAMPLE_KANE = """穆先生，您好！
钢结构支撑座高320mm，调整BF41滤洗机支腿长度如附件所示，请查收。
谢谢！

Best regards
康文胜/Kane
项目工程部
派特克工程设计咨询（厦门）有限公司
厦门市思明区湖滨南路90号立信广场1709室
Rm1709，17thFl.,Lixin Square,No.90,Hubin South Road,Xiamen361004,China
T:+86-592-3299652
F:+86-592-3299580
Email:kanek@cicorp.com.tw

-----邮件原件-----
发件人: ycmu@ecic.com.tw [mailto:ycmu@ecic.com.tw]
发送时间: 2026年7月7日 19:10
收件人: hansony@cicorp.com.tw
抄送: 'CIC/Allen PK Chen'; 'CIC/Catherine Li'; ctray@ecic.com.tw
主题: BF41濾洗器澄清圖面

于先生好

濾洗器BF41圖面提供如下，此份文件JSR澄清中(還未簽認)...
"""

_SAMPLE_TZUYU = """Candy 好

宗紘回報不能用的時候，他權限是如你的截圖
我現在把PMC的權限也加給他，如下圖


Best Regards

劉子瑜   敬上
台灣永光化學工業股份有限公司
工程管理暨智慧製造處 智慧製造部
地址：桃園市大園區中山北路271號
電話：03-386-8081 Ext.931
E-mail：tzuyu@ecic.com.tw


"Candy Chao" ---2026/07/08 下午 03:17:19---Hi 子瑜,可以先幫我試試看這樣夠不夠嗎? [cid:a29930b7]

寄件人: "Candy Chao" <CHIHHSUAN.CHAO@rockwellautomation.com>
收件者: "tzuyu@ecic.com.tw" <tzuyu@ecic.com.tw>
副本抄送： "Alex Hong" <ChengFong.Hong@rockwellautomation.com>
日期： 2026/07/08 下午 03:17
主旨： Re: Re: Re: EXTERNAL: MES abort 執行畫面abort權限

Hi 子瑜,可以先幫我試試看這樣夠不夠嗎?
"""


_SAMPLE_HUNG = """回覆: 巡視各棟內外陰井、雨水溝結果
各位主管好:

A棟與外勞宿舍陰井皆已修復完成，

外勞宿舍陰井主因是垃圾太多堵塞，目前工程有清理泵堵塞陰井恢復正常，

請後續外勞宿舍與餐廳陰井相關單位協調定期處理，謝謝

洪建旭---2026/07/09 上午 10:46:11---各位主管好： 巡視各棟內外陰井、雨水溝結果如下：

各位主管好：
巡視各棟內外陰井、雨水溝結果如下：
"""


def _run_self_test():
    print("=== self-test: kane (BF41) ===")
    out, sender, date = strip_quoted_history_with_identity(_SAMPLE_KANE)
    print(out)
    print(f"--- 原長 {len(_SAMPLE_KANE)} -> 清完 {len(out)}，被引用者=({sender!r}, {date!r}) ---\n")
    assert sender == "ycmu@ecic.com.tw", sender
    assert date == "2026年7月7日 19:10", date

    print("=== self-test: 劉子瑜 (MES abort) ===")
    out, sender, date = strip_quoted_history_with_identity(_SAMPLE_TZUYU)
    print(out)
    print(f"--- 原長 {len(_SAMPLE_TZUYU)} -> 清完 {len(out)}，被引用者=({sender!r}, {date!r}) ---\n")
    assert sender == "Candy Chao", sender
    assert date == "2026/07/08 下午 03:17:19", date

    print("=== self-test: 蔡道明回覆洪建旭 (巡視各棟內外陰井，無引號版本) ===")
    out, sender, date = strip_quoted_history_with_identity(_SAMPLE_HUNG)
    print(out)
    print(f"--- 原長 {len(_SAMPLE_HUNG)} -> 清完 {len(out)}，被引用者=({sender!r}, {date!r}) ---\n")
    assert sender == "洪建旭", sender
    assert date == "2026/07/09 上午 10:46:11", date

    print("全部自我測試通過 ✓")


if __name__ == "__main__":
    _run_self_test()
