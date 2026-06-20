---
name: android-stop
description: >-
  關閉 Android 模擬器與 Android Studio。當用戶說「關閉模擬器」、「關閉 Android」、
  「關掉虛擬機」、「關閉 Android Studio」時使用此 skill。
---

# Android 模擬器 & Android Studio 關閉

## 執行步驟

### Step 1：關閉模擬器

```bash
/Users/shuhsing/Library/Android/sdk/platform-tools/adb -s emulator-5554 shell reboot -p
```

### Step 2：確認模擬器已離線

```bash
/Users/shuhsing/Library/Android/sdk/platform-tools/adb devices
```

回傳清單為空即代表關閉成功。

### Step 3：關閉 Android Studio（若有開啟）

```bash
osascript -e 'tell application "Android Studio" to quit'
```

若回傳錯誤（使用者取消 / 已關閉），改用：

```bash
pgrep -f "studio" | head -5
```

確認是否還有 process，有的話：

```bash
pkill -f "Android Studio"
```

## 結果呈現

```
✅ 模擬器已關閉。
✅ Android Studio 已關閉。（若有開啟）
```

## Changelog

- 1.0.0 (2026-06-04): 初始版本
