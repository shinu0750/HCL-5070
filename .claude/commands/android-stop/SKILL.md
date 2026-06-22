---
name: android-stop
description: >-
  關閉 Android 模擬器與 Android Studio。當用戶說「關閉模擬器」、「關閉 Android」、
  「關掉虛擬機」、「關閉 Android Studio」時使用此 skill。
---

# Android 模擬器 & Android Studio 關閉

## 執行步驟

### Step 1：關閉模擬器

```powershell
& "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe" -s emulator-5554 shell reboot -p
```

### Step 2：確認模擬器已離線

```powershell
Start-Sleep 3
& "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe" devices
```

若仍顯示 `offline`，再等 5 秒重確認：

```powershell
Start-Sleep 5
& "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe" devices
```

回傳清單為空即代表關閉成功。

### Step 3：關閉 Android Studio（若有開啟）

```powershell
Stop-Process -Name "studio64" -Force -ErrorAction SilentlyContinue
Get-Process -Name "studio64" -ErrorAction SilentlyContinue | Select-Object Name, Id
```

若 Get-Process 無輸出或 exit 1 → Android Studio 未開啟，屬正常。

## 結果呈現

```
✅ 模擬器已關閉。
✅ Android Studio 已關閉。（若有開啟）
```

## Changelog

- 3.0.0 (2026-06-21): 全面改用 PowerShell — 路徑改為 Windows 原生 `C:\Users\EID\AppData\Local\Android\Sdk\`；移除 bash/WSL 路徑；Step 2 加入 offline 等待邏輯
- 2.0.0 (2026-06-21): 移植至 Windows/WSL — 路徑改為 WSL `/mnt/c/Users/EID/AppData/Local/Android/Sdk/`；`osascript`/`pkill` 改為 `powershell.exe Stop-Process`
- 1.0.0 (2026-06-04): 初始版本
