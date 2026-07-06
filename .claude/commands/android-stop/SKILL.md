---
name: android-stop
description: >-
  關閉 Android 模擬器（目前有哪台開著就關哪台，支援 ShuHsing/tzuyu 多台同時開）
  與 Android Studio。當用戶說「關閉模擬器」、「關閉 Android」、「關掉虛擬機」、
  「關閉 Android Studio」、「關閉全部模擬器」時使用此 skill。
version: 4.0.0
---

# Android 模擬器 & Android Studio 關閉（支援多台）

已知模擬器 serial 見 `android-start` skill 的「已知裝置對照表」（目前 `emulator-5554`=ShuHsing、
`emulator-5556`=tzuyu）。這支 skill **不假設只有一台在跑**，先查目前實際連線的裝置，逐一關閉。

## 執行步驟

### Step 1：查目前實際連線的裝置

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
$devices = (& $adb devices) | Select-String "^emulator-\d+\s+device$" | ForEach-Object { ($_ -split "\s+")[0] }
$devices
```

沒有任何輸出 → 沒有模擬器在跑，跳到 Step 3。

### Step 2：逐一關閉每台模擬器

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
foreach ($serial in $devices) {
    Write-Output "關閉 $serial ..."
    & $adb -s $serial shell reboot -p
}
Start-Sleep 3
& $adb devices
```

若還有殘留 `offline`，再等 5 秒重確認：

```powershell
Start-Sleep 5
& "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe" devices
```

回傳清單為空即代表全部關閉成功。

### Step 3：關閉 Android Studio（若有開啟）

```powershell
Stop-Process -Name "studio64" -Force -ErrorAction SilentlyContinue
Get-Process -Name "studio64" -ErrorAction SilentlyContinue | Select-Object Name, Id
```

若 Get-Process 無輸出或 exit 1 → Android Studio 未開啟，屬正常。

> 模擬器透過 `emulator.exe`／Device Manager 播放鍵啟動時是獨立程序，不掛在 `studio64.exe` 底下，
> 關閉 Android Studio 不會連帶關閉還在跑的模擬器——所以 Step 2 一定要在 Step 3 之前做，
> 不能只關 Android Studio 就以為模擬器也關了。

## 只想關閉單一台時

跟用戶確認要關哪一台（例如只關 tzuyu，留 ShuHsing 繼續跑），Step 2 直接指定該 serial：

```powershell
& "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe" -s emulator-5556 shell reboot -p
```

這種情況下 Step 3（關 Android Studio）通常要跳過，除非用戶明確要整個關掉。

## 結果呈現

```
✅ 已關閉：emulator-5554, emulator-5556（依實際偵測到的裝置列出）
✅ Android Studio 已關閉。（若有開啟）
```

## Changelog

- 4.0.0 (2026-07-05): 支援多台模擬器同時關閉
  - 不再寫死只關 `emulator-5554`，改成先用 `adb devices` 查目前實際連線的裝置，逐一關閉
  - 補上「只想關閉單一台」的用法，跟 `android-start` 的多帳號情境對應
  - 補充說明：模擬器程序獨立於 Android Studio，關 Studio 不會連帶關模擬器，Step 2 順序不能省略
- 3.0.0 (2026-06-21): 全面改用 PowerShell — 路徑改為 Windows 原生 `C:\Users\EID\AppData\Local\Android\Sdk\`；移除 bash/WSL 路徑；Step 2 加入 offline 等待邏輯
- 2.0.0 (2026-06-21): 移植至 Windows/WSL — 路徑改為 WSL `/mnt/c/Users/EID/AppData/Local/Android/Sdk/`；`osascript`/`pkill` 改為 `powershell.exe Stop-Process`
- 1.0.0 (2026-06-04): 初始版本
