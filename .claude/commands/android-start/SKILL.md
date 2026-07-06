---
name: android-start
description: >-
  啟動 Android 模擬器並左轉螢幕 90°，支援多個帳號測試機（自己/ShuHsing、
  同事帳號測試/tzuyu，可再擴充）。當用戶說「開啟模擬器」、「啟動 Android」、
  「開 Android」、「開虛擬機」、「開 tzuyu」、「開同事的模擬器」時使用此 skill。
version: 4.0.0
---

# Android 模擬器啟動 + 左轉 90°（參數化，支援多帳號）

## 已知裝置對照表

| 代號 | AVD 名稱 | Port | Serial | 用途 |
|------|----------|------|--------|------|
| 預設（不指名時） | ShuHsing | 5554 | emulator-5554 | 自己的帳號 |
| tzuyu | tzuyu | 5556 | emulator-5556 | 同事帳號測試（`hcl-notes-approval` 代簽） |

**新增第三人時**：在這個表格加一列即可，挑一個沒人用過的偶數 port（例如 5558）。
不用新增 skill 檔案，也不用改任何程式碼——下面所有步驟都用 `$AVD` / `$PORT` / `$SERIAL`
三個變數代入，跟著表格走就對。

## 判斷這次要開哪一台

- 用戶沒指名（「開模擬器」「啟動 Android」）→ 用「預設」那一列（ShuHsing / 5554）
- 用戶提到 tzuyu / 同事帳號 / 對方帳號 → 用 tzuyu 那一列
- 用戶提到表格裡沒有的名字 → 先問清楚是要開新的一台（順便問要不要記錄到表格）還是打錯字

決定好後，把下面步驟裡的 `$AVD`、`$PORT`、`$SERIAL` 換成對應值再執行。

---

## 執行步驟

### Step 1：啟動模擬器

```powershell
Start-Process -FilePath "C:\Users\EID\AppData\Local\Android\Sdk\emulator\emulator.exe" -ArgumentList "-avd $AVD -port $PORT -gpu swiftshader_indirect -no-snapshot-load -no-audio" -WindowStyle Normal
```

> `-port $PORT` 一定要明確帶，不能省略——不釘死 port 的話，誰先開機就搶到 5554，
> 兩台以上同時存在時 serial 會對錯裝置（`hcl-notes-approval` 系列腳本靠 serial 認裝置）。

### Step 2：等待開機完成

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
for ($i = 1; $i -le 60; $i++) {
    $result = & $adb -s $SERIAL shell getprop sys.boot_completed 2>$null
    $result = $result -replace '\r','' -replace '\n',''
    Write-Output "[$i] boot_completed=$result"
    if ($result -eq "1") { Write-Output "BOOTED"; break }
    Start-Sleep 3
}
```

timeout 200 秒。複製來的 AVD（例如 tzuyu）沒有 snapshot，冷開機可能跑滿全部秒數。

### Step 3：第一次啟動 Verse（用來觸發它釋放 rotation lock，然後我們覆蓋掉）

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
& $adb -s $SERIAL shell monkey -p com.lotus.sync.traveler -c android.intent.category.LAUNCHER 1
Start-Sleep 5
```

> Verse 啟動時會把 `accelerometer_rotation=1`、`user_rotation=0` 蓋過去（SinglePaneMailActivity manifest 行為）。
> 必須先讓它跑完啟動程序，否則之後鎖的旋轉會被它清掉。

### Step 4：鎖定橫向（rotation=1）

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
& $adb -s $SERIAL shell settings put system accelerometer_rotation 0
& $adb -s $SERIAL shell wm user-rotation lock 1
& $adb -s $SERIAL shell settings put system user_rotation 1
Start-Sleep 2
```

> ⚠️ `wm user-rotation lock 1` 只設 lock 模式，**不會主動觸發旋轉**；
> 必須再下 `settings put system user_rotation 1` 才會把 display 轉到 2400×1080。

### Step 5：強制重啟 Verse + 再套一次旋轉，讓它在橫向狀態下重新排版

```powershell
$adb = "C:\Users\EID\AppData\Local\Android\Sdk\platform-tools\adb.exe"
& $adb -s $SERIAL shell am force-stop com.lotus.sync.traveler
Start-Sleep 1
& $adb -s $SERIAL shell monkey -p com.lotus.sync.traveler -c android.intent.category.LAUNCHER 1
Start-Sleep 6
# force-stop + relaunch 會清掉 user_rotation，必須再套一次
& $adb -s $SERIAL shell settings put system accelerometer_rotation 0
& $adb -s $SERIAL shell settings put system user_rotation 1
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
$tmp = "C:\Users\EID\AppData\Local\Temp\verify_$AVD.png"
& $adb -s $SERIAL shell screencap -p //sdcard/verify.png
& $adb -s $SERIAL pull //sdcard/verify.png $tmp
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($tmp)
Write-Output "Size: $($img.Width) x $($img.Height)"
$img.Dispose()
```

> Windows 上用 Git Bash 執行 adb 時，`/sdcard/...` 這種絕對路徑會被 MSYS 誤轉成本機路徑
> （例如變成 `C:/Program Files/Git/sdcard/...`），要用 `//sdcard/...`（雙斜線）避開。PowerShell 原生不會有這問題。

回傳 `Size: 2400 x 1080` 且 Verse UI 應正常橫向顯示（標題列在上、信件列表水平排列）。
若文字仍直書 → 表示 Step 5 漏掉，重跑 Step 5。

用 Read tool 讀取截圖確認畫面正常：
```
Read: C:\Users\EID\AppData\Local\Temp\verify_$AVD.png
```

## 結果呈現

```
✅ $AVD 模擬器已啟動（$SERIAL），rotation=1 橫向，可以使用。
```

## 接續使用（代簽別人帳號時）

跑 `hcl-notes-approval` 對應這台裝置的流程前，記得設環境變數（見該 skill 的說明）：

```powershell
$env:HCL_ADB_SERIAL = "$SERIAL"
$env:HCL_USERNAME = "..."
$env:HCL_PASSWORD = "..."
$env:HCL_NOTES_PASSWORD = "..."
```

## Changelog

- 4.0.0 (2026-07-05): 改為參數化，合併 `android-start-tzuyu`
  - 原本一人一個 skill 檔案（`android-start` 只認 ShuHsing、另建 `android-start-tzuyu` 認 tzuyu）
    改成單一 skill + 「已知裝置對照表」，新增第三人只要加表格一列，不用再開新檔案
  - 所有步驟改用 `$AVD` / `$PORT` / `$SERIAL` 變數代入
  - Step 6 截圖路徑補上 Git Bash 下 `/sdcard` 路徑會被 MSYS 誤轉的說明與 `//sdcard` workaround
- 3.1.0 (2026-07-05): 啟動指令補上 `-port 5554`，避免與 tzuyu（同事帳號測試機）搶 port 造成 serial 對錯裝置
- 3.0.0 (2026-06-21): 全面改用 PowerShell — 路徑改為 Windows 原生 `C:\Users\EID\AppData\Local\Android\Sdk\`；AVD 名稱改為 ShuHsing；Step 6 改用 .NET System.Drawing 讀圖尺寸（不依賴 Python/PIL）；截圖改存 Windows Temp 路徑
- 2.0.0 (2026-06-21): 移植至 Windows/WSL — 路徑改為 WSL `/mnt/c/Users/EID/AppData/Local/Android/Sdk/`；加入 `-gpu swiftshader_indirect` 停用 GPU
- 1.3.0 (2026-06-04): 修正旋轉時機 — Verse 啟動會釋放 rotation lock（accelerometer_rotation=1, user_rotation=0），必須先開 Verse 再鎖橫向；Step 3/4 對調順序，否則每次重開模擬器都會 fail
- 1.2.0 (2026-06-04): Step 3 補上 `settings put system user_rotation 1` — `wm user-rotation lock 1` 只設 lock 模式不會觸發旋轉，必須兩條都下 display 才會真的轉橫向
- 1.1.0 (2026-06-04): Step 3 改用 `wm user-rotation lock 1`；`emu rotate` 在 Android 14 無效
- 1.0.0 (2026-06-04): 初始版本
