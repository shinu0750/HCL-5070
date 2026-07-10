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
    re.compile(r'"[^"\n]{1,30}"\s*---\d{4}/\d{2}/\d{2}'),  # Notes: "名字" ---2026/07/08...---
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


def _run_self_test():
    print("=== self-test: kane (BF41) ===")
    out = strip_quoted_history(_SAMPLE_KANE)
    print(out)
    print(f"--- 原長 {len(_SAMPLE_KANE)} -> 清完 {len(out)} ---\n")

    print("=== self-test: 劉子瑜 (MES abort) ===")
    out = strip_quoted_history(_SAMPLE_TZUYU)
    print(out)
    print(f"--- 原長 {len(_SAMPLE_TZUYU)} -> 清完 {len(out)} ---\n")


if __name__ == "__main__":
    _run_self_test()
