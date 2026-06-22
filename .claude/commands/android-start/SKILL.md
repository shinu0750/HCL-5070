---
name: android-start
description: >-
  啟動 Android 模擬器（ShuHsing）並左轉螢幕 90°。當用戶說「開啟模擬器」、
  「啟動 Android」、「開 Android」、「開虛擬機」時使用此 skill。
---

# Android 模擬器啟動 + 左轉 90°

## 執行步驟

### Step 1：啟動模擬器

```powershell
Start-Process -FilePath "C:\Users\EID\AppData\Local\Android\Sdk\emulator\emulator.exe" -ArgumentList "-avd ShuHsing -gpu swiftshader_indirect -no-snapshot-load -no-audio" -WindowStyle Normal
```

### Step 2：等待開機完成

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
for ($i = 1; $i -le 60; $i++) {
    $result = & $adb shell getprop sys.boot_completed 2>$null
    $result = $result -replace '\r','' -replace '\n',''
    Write-Output "[$i] boot_completed=$result"
    if ($result -eq "1") { Write-Output "BOOTED"; break }
    Start-Sleep 3
}
```

timeout 200 秒。

### Step 3：第一次啟動 Verse（用來觸發它釋放 rotation lock，然後我們覆蓋掉）

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
& $adb -s emulator-5554 shell monkey -p com.lotus.sync.traveler -c android.intent.category.LAUNCHER 1
Start-Sleep 5
```

> Verse 啟動時會把 `accelerometer_rotation=1`、`user_rotation=0` 蓋過去（SinglePaneMailActivity manifest 行為）。
> 必須先讓它跑完啟動程序，否則之後鎖的旋轉會被它清掉。

### Step 4：鎖定橫向（rotation=1）

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
& $adb -s emulator-5554 shell settings put system accelerometer_rotation 0
& $adb -s emulator-5554 shell wm user-rotation lock 1
& $adb -s emulator-5554 shell settings put system user_rotation 1
Start-Sleep 2
```

> ⚠️ `wm user-rotation lock 1` 只設 lock 模式，**不會主動觸發旋轉**；
> 必須再下 `settings put system user_rotation 1` 才會把 display 轉到 2400×1080。

### Step 5：強制重啟 Verse + 再套一次旋轉，讓它在橫向狀態下重新排版

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
& $adb -s emulator-5554 shell am force-stop com.lotus.sync.traveler
Start-Sleep 1
& $adb -s emulator-5554 shell monkey -p com.lotus.sync.traveler -c android.intent.category.LAUNCHER 1
Start-Sleep 6
# force-stop + relaunch 會清掉 user_rotation，必須再套一次
& $adb -s emulator-5554 shell settings put system accelerometer_rotation 0
& $adb -s emulator-5554 shell settings put system user_rotation 1
Start-Sleep 2
```

> ⚠️ **兩件事不可省略**：
> 1. 如果 Verse 是在直向時啟動的，即使後來轉橫向，Verse 內部 UI 仍維持直向 layout
>    （文字直書、tap 座標對不到 UI 元件）。必須 force-stop 後重開。
> 2. `force-stop` + `monkey LAUNCHER` 重啟會把 `user_rotation` 清回 0，
>    所以重啟後必須**再套一次** rotation 設定。

### Step 6：確認

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
$tmp = "C:\Users\EID\AppData\Local\Temp\verify.png"
& $adb -s emulator-5554 shell screencap -p /sdcard/verify.png
& $adb -s emulator-5554 pull /sdcard/verify.png $tmp
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($tmp)
Write-Output "Size: $($img.Width) x $($img.Height)"
$img.Dispose()
```

回傳 `Size: 2400 x 1080` 且 Verse UI 應正常橫向顯示（標題列在上、信件列表水平排列）。
若文字仍直書 → 表示 Step 5 漏掉，重跑 Step 5。

用 Read tool 讀取截圖確認畫面正常：
```
Read: C:\Users\EID\AppData\Local\Temp\verify.png
```

## 結果呈現

```
✅ ShuHsing 模擬器已啟動，rotation=1 橫向，可以使用。
```

## Changelog

- 3.0.0 (2026-06-21): 全面改用 PowerShell — 路徑改為 Windows 原生 `C:\Users\EID\AppData\Local\Android\Sdk\`；AVD 名稱改為 ShuHsing；Step 6 改用 .NET System.Drawing 讀圖尺寸（不依賴 Python/PIL）；截圖改存 Windows Temp 路徑
- 2.0.0 (2026-06-21): 移植至 Windows/WSL — 路徑改為 WSL `/mnt/c/Users/EID/AppData/Local/Android/Sdk/`；加入 `-gpu swiftshader_indirect` 停用 GPU
- 1.3.0 (2026-06-04): 修正旋轉時機 — Verse 啟動會釋放 rotation lock（accelerometer_rotation=1, user_rotation=0），必須先開 Verse 再鎖橫向；Step 3/4 對調順序，否則每次重開模擬器都會 fail
- 1.2.0 (2026-06-04): Step 3 補上 `settings put system user_rotation 1` — `wm user-rotation lock 1` 只設 lock 模式不會觸發旋轉，必須兩條都下 display 才會真的轉橫向
- 1.1.0 (2026-06-04): Step 3 改用 `wm user-rotation lock 1`；`emu rotate` 在 Android 14 無效
- 1.0.0 (2026-06-04): 初始版本
