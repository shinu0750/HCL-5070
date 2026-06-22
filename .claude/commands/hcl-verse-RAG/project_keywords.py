# 關鍵字→專案對照表
# v4 (2026-06-11)：加權 scoring + threshold + multi-tag
#   每個關鍵字附權重 3/2/1（distinctive/medium/generic）
#   match_project：回傳最高分（threshold 過濾，向後相容）
#   match_projects：回傳所有達 threshold 的 proj（top 與 runner-up 差距 ≤ delta 並列）
# 變更紀錄見檔尾 CHANGELOG

# 權重哲學：
# 3 = 專屬詞，命中幾乎必定就是該專案（產品名、案號、廠商代號、樓棟+專案組合）
# 2 = 領域核心詞，跨案但意義集中
# 1 = 領域泛詞或產業通用，命中要靠其他詞輔助
import re as _re

# 文字正規化：把「X 棟」/「X　棟」壓成「X棟」、「一 廠」壓成「一廠」
# 解決中文文本裡英字+全/半形空格+棟、廠號中間有空格導致 keyword 漏抓
_NORMALIZE_RE_BUILDING = _re.compile(r'([A-Za-z])[\s　]+棟')
_NORMALIZE_RE_FACTORY  = _re.compile(r'([一二三四])[\s　]+廠')

def _normalize(text: str) -> str:
    text = _NORMALIZE_RE_BUILDING.sub(r'\1棟', text)
    text = _NORMALIZE_RE_FACTORY.sub(r'\1廠', text)
    return text


PROJECT_KEYWORDS: dict[str, dict[str, int]] = {
    "PharmaSuite/MES": {
        "PharmaSuite": 3, "ThinManager": 3, "8D": 3, "workcenter": 3,
        "MES": 2, "配方": 2, "171": 2, "807": 2,
    },
    "一廠D棟洗瓶機產線": {
        "洗瓶機": 3,
        "D棟無塵室": 3, "一廠D棟": 3, "D棟產線": 3,
        "帆宣": 1, "無塵室": 1,
    },
    "四廠B棟CCL專案": {
        "CCL": 3, "HVM": 3,
    },
    "電化P棟JSR三期": {
        "JSR三期": 3, "JSR第三期": 3, "第3期JSR": 3, "JSR P棟": 3,
        "JSR代工": 3, "JSR委託代工": 3, "JSR新案": 3, "JSR Meeting": 3,
        "電化廠P棟4/5樓": 3, "P4-5F": 3,
    },
    "ESCO專案": {
        "ESCO": 3, "冰水機": 3, "能源管理": 3,
        "節能": 1, "能源": 1,
    },
    "電化廠防爆改善": {
        "電化廠防爆": 3, "電磁閥": 3,
        "防爆": 2,
    },
    "充填機": {
        "充填機": 3,
    },
    "一廠J棟Batch": {
        "Batch control": 3, "一廠J棟": 3, "一廠 J棟": 3,
        "J棟輸冰": 3, "J棟Batch": 3, "J棟HMI": 3, "J棟 HMI": 3,
        "J棟PFD": 3, "J棟 PFD": 3, "J棟網路": 3,
    },
    "電化LIMS": {
        "LIMS": 3, "電化LIMS": 3,
    },
    "IOT軟硬體": {
        "物聯網": 3, "邊緣運算": 3,
        "IOT": 2, "感測": 1,
    },
    "儀控工程": {
        "儀控": 2, "儀表": 1, "控制工程": 2,
    },
    "電化技服隔間": {
        "V103": 3, "律准": 3, "技服隔間": 3, "隔間工程": 3,
    },
    "公危品電子紙看板": {
        "公危品": 3, "電子紙": 3,
        "看板": 1,
    },
    "物流無人車": {
        "AGV": 3, "陽程": 3, "物流無人車": 3, "無人車": 3,
    },
    "三廠L棟復建": {
        "三廠L棟": 3, "復建": 2,
    },
    "物流倉儲評估": {
        "WMS": 3, "Kalypso": 3, "物流倉儲": 3,
        "倉儲": 1,
    },
    "電化V棟JSR二期": {
        "V3F": 3, "JSR二期": 3, "MX2001": 3, "SFAT": 3,
        "V3擴建": 3, "V3F擴建": 3, "V棟消防": 3,
    },
}

THRESHOLD = 2     # 總分 < 2 視為未分類
DELTA     = 1     # runner-up 與 top 差距 ≤ DELTA 時並列貼 multi-tag


def score_projects(subject: str, body: str = "") -> dict[str, tuple[int, list[str]]]:
    """
    回傳每個專案的 (總分, 命中關鍵字 list)。只包含分數 > 0 的專案。
    """
    text = _normalize(f"{subject} {body}")
    out: dict[str, tuple[int, list[str]]] = {}
    for proj, kw_weights in PROJECT_KEYWORDS.items():
        matched = [(kw, w) for kw, w in kw_weights.items() if kw in text]
        if matched:
            total = sum(w for _, w in matched)
            out[proj] = (total, [kw for kw, _ in matched])
    return out


def match_projects(
    subject: str, body: str = "",
    threshold: int = THRESHOLD, delta: int = DELTA,
) -> list[str]:
    """
    回傳所有應該貼的 proj：分數 ≥ threshold，且與最高分差距 ≤ delta。
    無命中或全部低於 threshold 回傳 []。
    """
    scores = score_projects(subject, body)
    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    top_score = ranked[0][1][0]
    if top_score < threshold:
        return []
    return [proj for proj, (s, _) in ranked if s >= threshold and (top_score - s) <= delta]


def match_project(subject: str, body: str = "") -> str | None:
    """
    向後相容：回傳最高分專案（若有並列以 PROJECT_KEYWORDS 順序為準）。
    """
    projs = match_projects(subject, body)
    return projs[0] if projs else None


# CHANGELOG
# v4.2 (2026-06-11): 空格正規化 + 帆宣降權
#   _normalize: 「X 棟」→「X棟」、「一 廠」→「一廠」（修 recall）
#   帆宣 3→1（廠商跨多案，weight 太重會把無關信全拖到 D棟）
# v4.1 (2026-06-11): targeted補丁 — D棟無塵室/一廠D棟/D棟產線/能源管理
# v4 (2026-06-11): scoring + threshold + multi-tag
#   keyword → weight（3 distinctive / 2 medium / 1 generic）
#   THRESHOLD=2、DELTA=1（runner-up 在 top-1 內並列）
# v3 (2026-06-11): augment（recall fix）— JSR三期/J棟Batch/JSR二期 補多字詞
# v2 (2026-06-11): denoise（precision fix）— 移除樓棟單字、RA、產業泛詞
# v1 (2026-06-11): 初版 17 專案 + first-match-wins
