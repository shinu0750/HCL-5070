#!/usr/bin/env python3
"""
獨立模組：追蹤「email_mapping 查不到的人」（外部聯絡人/離職同仁不分類，
統一判斷條件是「查不到」）。

負責記錄 email + 這次看到的顯示名 + 次數 + 時間範圍到
external_contacts_state.json，供之後產生 Excel / Google Chat 通知使用。
這支只管「記錄」，不管 Excel 產生、不管 Hindsight/Qdrant 回填
（那些之後在 update_external_contacts.py 處理）。

之後要整合進 verse_archive_pipeline.py 的 resolve_sender()：
查 email_mapping 沒查到時呼叫 track_unknown_contact()。
"""
import os
import json

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "external_contacts_state.json")


def load_state(path=None):
    path = path or STATE_FILE
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state, path=None):
    path = path or STATE_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def track_unknown_contact(email, display_name, date_str, state):
    """
    記錄一個 email_mapping 查不到的人。
    email 不在 state -> 新增一筆；已在 -> 累加次數、把新出現的顯示名加進
    seen_names（去重）、更新 first_seen/last_seen。

    state 是呼叫方傳入、跨多次呼叫共用的 dict（同一次 pipeline run 內只要
    存一次檔即可，不用每則訊息都寫檔）。就地修改並回傳同一個物件。
    """
    email = (email or "").strip().lower()
    display_name = (display_name or "").strip()
    if not email:
        return state

    entry = state.get(email)
    if entry is None:
        state[email] = {
            "seen_names": [display_name] if display_name else [],
            "count": 1,
            "first_seen": date_str or "",
            "last_seen": date_str or "",
            "confirmed": False,
        }
        return state

    entry["count"] += 1
    if display_name and display_name not in entry["seen_names"]:
        entry["seen_names"].append(display_name)
    if date_str:
        # 日期格式是 YYYY-MM-DD(HH:MM)，字典序等於時間序，直接比字串就好
        if not entry["first_seen"] or date_str < entry["first_seen"]:
            entry["first_seen"] = date_str
        if not entry["last_seen"] or date_str > entry["last_seen"]:
            entry["last_seen"] = date_str
    return state


def get_pending_contacts(state):
    """回傳所有 confirmed=false 的聯絡人（給 Excel 產生器用）。"""
    return {email: info for email, info in state.items() if not info.get("confirmed")}


def has_new_or_updated(old_state, new_state):
    """比較兩次 state，判斷是否有新 email 或既有 email 的 seen_names/count 有變化
    （用來決定要不要重新產生 Excel + 發通知，沒變化就不用吵）。"""
    for email, info in new_state.items():
        old = old_state.get(email)
        if old is None:
            return True
        if set(info.get("seen_names", [])) != set(old.get("seen_names", [])):
            return True
        if info.get("count") != old.get("count"):
            return True
    return False


def _run_self_test():
    import tempfile
    tmp_path = os.path.join(tempfile.mkdtemp(), "test_external_contacts_state.json")

    state = load_state(tmp_path)  # 空檔案 -> {}
    print(f"初始 state: {state}")

    state = track_unknown_contact("kanek@cicorp.com.tw", "kane", "2026-07-07 15:00", state)
    state = track_unknown_contact("kanek@cicorp.com.tw", "康文胜/Kane", "2026-07-08 15:56", state)
    state = track_unknown_contact("kanek@cicorp.com.tw", "kane", "2026-07-08 10:41", state)  # 重複名字，不該多存一次
    state = track_unknown_contact("sam@vendor.com", "Sam Lee", "2026-07-09", state)
    save_state(state, tmp_path)

    reloaded = load_state(tmp_path)
    print("\n=== 存檔後重新讀取 ===")
    print(json.dumps(reloaded, ensure_ascii=False, indent=2))

    assert reloaded["kanek@cicorp.com.tw"]["count"] == 3
    assert set(reloaded["kanek@cicorp.com.tw"]["seen_names"]) == {"kane", "康文胜/Kane"}
    assert reloaded["kanek@cicorp.com.tw"]["first_seen"] == "2026-07-07 15:00"
    assert reloaded["kanek@cicorp.com.tw"]["last_seen"] == "2026-07-08 15:56"
    assert reloaded["sam@vendor.com"]["count"] == 1
    print("\n✓ 累加/去重/first_seen/last_seen 驗證通過")

    pending = get_pending_contacts(reloaded)
    print(f"\n待確認：{len(pending)} 位 -> {list(pending.keys())}")

    reloaded["kanek@cicorp.com.tw"]["confirmed"] = True
    save_state(reloaded, tmp_path)
    pending_after = get_pending_contacts(load_state(tmp_path))
    assert "kanek@cicorp.com.tw" not in pending_after
    print(f"標記 kane 為已確認後，待確認剩：{list(pending_after.keys())}")

    print("\n=== has_new_or_updated 測試 ===")
    old = load_state(tmp_path)
    new = dict(old)
    print("沒變化時:", has_new_or_updated(old, json.loads(json.dumps(old))))  # False
    changed = json.loads(json.dumps(old))
    changed["sam@vendor.com"]["count"] += 1
    print("count 變化時:", has_new_or_updated(old, changed))  # True

    print("\n全部自我測試通過 ✓")


if __name__ == "__main__":
    _run_self_test()
