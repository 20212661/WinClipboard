# WinClipboard per-user installer (no admin required).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File installer.ps1
#   installer.ps1 -InstallDir D:\apps\wclb -NoDesktop
#
# What it does (validation first, mutation last):
#   0. resolves the install target and refuses dangerous locations
#      (roots / personal folder roots / Windows / Program Files / the project
#      or build source dirs); a non-empty target must carry a valid
#      WinClipboard install marker; reparse points inside it are refused
#   0b. cross-checks the app version against dist build metadata
#   1. stops only THIS install's running instance (graceful first; aborts and
#      asks the user to close it unless -ForceStop)
#   2. mirrors dist\WinClipboard -> InstallDir (/MIR only into a confirmed
#      app-owned directory; /XJ never crosses junctions)
#   3. (re)writes the ownership marker; creates Start Menu + Desktop shortcuts
#      (opt out with -NoStartMenu / -NoDesktop)
#   4. writes a self-validating uninstall.ps1 into InstallDir
#   5. registers a per-user "Add/Remove Programs" entry pointing at that
#      uninstaller (nothing is registered unless the copy succeeded)
#
# Build the app first with:  打包构建.bat  (needs dist\WinClipboard\WinClipboard.exe)
# Keep this file pure ASCII (PowerShell 5.1 reads no-BOM scripts as ANSI).

param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\WinClipboard",
    [switch]$NoDesktop,
    [switch]$NoStartMenu,
    [string]$Version = "",
    # non-empty => registry key gets a suffix (used by automated tests)
    [string]$RegSuffix = "",
    # explicit override: force-stop this install's instances after the
    # graceful close attempt failed (never stops same-named other installs)
    [switch]$ForceStop
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "installer_safety.ps1")

$metaPath = Join-Path $PSScriptRoot "app_meta.py"
if ([string]::IsNullOrWhiteSpace($Version)) {
    if (-not (Test-Path -LiteralPath $metaPath)) {
        throw "app_meta.py not found; cannot determine application version."
    }
    $metaText = Get-Content -LiteralPath $metaPath -Raw
    $match = [regex]::Match($metaText, '(?m)^APP_VERSION\s*=\s*["'']([^"'']+)["'']\s*$')
    if (-not $match.Success) {
        throw "APP_VERSION not found in app_meta.py."
    }
    $Version = $match.Groups[1].Value
}
$src = Resolve-CanoPath (Join-Path $PSScriptRoot "dist\WinClipboard")
if ($null -eq $src -or -not (Test-Path -LiteralPath (Join-Path $src "WinClipboard.exe") -PathType Leaf)) {
    Write-Host "[ERROR] dist\WinClipboard\WinClipboard.exe not found. Run the build script first." -ForegroundColor Red
    exit 1
}

# ---- 0) target validation: BEFORE stopping processes, copying, registering ----
$InstallDir = Resolve-CanoPath $InstallDir
if ($null -eq $InstallDir) {
    Write-Host "[ERROR] install target cannot be resolved." -ForegroundColor Red
    exit 1
}
$reason = Get-UnsafeTargetReason $InstallDir $src $PSScriptRoot
if ($reason) {
    Write-Host "[ERROR] refusing unsafe install target: $reason" -ForegroundColor Red
    exit 1
}
$targetExe = Join-Path $InstallDir "WinClipboard.exe"
$existingMarker = $null
if (Test-Path -LiteralPath $InstallDir -PathType Container) {
    $hasAny = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue).Count -gt 0
    if ($hasAny) {
        if (-not (Test-InstallMarkerValid $InstallDir)) {
            Write-Host "[ERROR] target is not empty and carries no valid WinClipboard install marker." -ForegroundColor Red
            Write-Host "        Refusing to mirror into a directory this app does not own." -ForegroundColor Red
            exit 1
        }
        $existingMarker = Read-InstallMarker $InstallDir
        Write-Host "Target verified against install marker (update in place)."
    }
    $rp = Find-ReparsePoints $InstallDir
    if ($rp.Count -gt 0) {
        Write-Host "[ERROR] reparse points/junctions found inside the install target; refusing /MIR." -ForegroundColor Red
        exit 1
    }
}

# ---- 0b) build metadata must match the version being installed ----
$buildMeta = Join-Path $src "build-verification.json"
if (Test-Path -LiteralPath $buildMeta -PathType Leaf) {
    try {
        $bm = Get-Content -LiteralPath $buildMeta -Raw | ConvertFrom-Json
        if ($bm.version -and ([string]$bm.version -ne $Version)) {
            Write-Host ("[ERROR] dist build metadata says v{0} but app_meta.py says v{1}; rebuild before installing." -f $bm.version, $Version) -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "[WARN] build-verification.json unreadable; skipping version cross-check." -ForegroundColor Yellow
    }
} else {
    Write-Host "[WARN] dist\WinClipboard\build-verification.json missing; cannot cross-check build version." -ForegroundColor Yellow
}

# ---- 1) stop only this install's instances (graceful; ask, don't sweep) ----
function Get-ThisInstallIds {
    param([string]$Exe)
    return Get-TargetProcessIds @(Get-Process WinClipboard -ErrorAction SilentlyContinue) $Exe
}
$ids = Get-ThisInstallIds $targetExe
if (@($ids).Count -gt 0) {
    Write-Host ("Closing this install's running instance(es): " + ($ids -join ", "))
    foreach ($p in @(Get-Process WinClipboard -ErrorAction SilentlyContinue)) {
        if ($ids -contains $p.Id) {
            try { $p.CloseMainWindow() | Out-Null } catch { }
        }
    }
    $deadline = (Get-Date).AddSeconds(4)
    do {
        Start-Sleep -Milliseconds 400
        $ids = Get-ThisInstallIds $targetExe
    } while ((@($ids).Count -gt 0) -and ((Get-Date) -lt $deadline))
    if (@($ids).Count -gt 0) {
        if (-not $ForceStop) {
            Write-Host "[ERROR] a WinClipboard instance from this install directory is still running." -ForegroundColor Red
            Write-Host "        Exit it via the tray icon and run the installer again (or pass -ForceStop)." -ForegroundColor Red
            exit 1
        }
        Write-Host "Force-stopping after failed graceful close: $($ids -join ', ')"
        foreach ($i in $ids) {
            try { Stop-Process -Id $i -Force -ErrorAction SilentlyContinue } catch { }
        }
        Start-Sleep -Milliseconds 800
    }
}

# ---- 2) mirror into the confirmed directory ----
Write-Host "Installing to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
robocopy $src $InstallDir /MIR /XJ /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "[ERROR] file copy failed (robocopy code $LASTEXITCODE); nothing was registered." -ForegroundColor Red
    exit 1
}
if ($LASTEXITCODE -ge 2) {
    Write-Host ("[WARN] robocopy reported cleanup warnings (code {0}); some extra files in the target could not be removed." -f $LASTEXITCODE) -ForegroundColor Yellow
}

# ---- 3) ownership marker (stable instance id across updates) ----
$instanceId = ""
if ($existingMarker -and $existingMarker.instanceId) {
    $instanceId = [string]$existingMarker.instanceId
}
$instanceId = New-InstallMarker $InstallDir $Version $instanceId

# ---- 4) shortcuts / uninstaller / registry (only after a successful copy) ----
$exe = $targetExe
$ws = New-Object -ComObject WScript.Shell

function Make-Shortcut([string]$path) {
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $InstallDir
    $lnk.Description = "WinClipboard clipboard manager"
    $lnk.Save()
}

if (-not $NoStartMenu) {
    $sm = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\WinClipboard.lnk"
    Make-Shortcut $sm
    Write-Host "Start menu shortcut: $sm"
}
if (-not $NoDesktop) {
    $dt = Join-Path ([Environment]::GetFolderPath("Desktop")) "WinClipboard.lnk"
    Make-Shortcut $dt
    Write-Host "Desktop shortcut: $dt"
}

# Generated uninstaller: re-validates ownership before deleting anything,
# stops only its own instance, removes only shortcuts pointing to this exe.
# Single-quoted here-string: content is fully literal, no escaping games.
$uninst = Join-Path $InstallDir "uninstall.ps1"
@'
# WinClipboard uninstaller (generated by installer.ps1 - do not edit)
param([string]$RegSuffix = "", [switch]$ForceStop)
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
function Trim-Seps([string]$p) {
    while ($p.Length -gt 3 -and $p.EndsWith('\')) { $p = $p.Substring(0, $p.Length - 1) }
    return $p
}
# 1) ownership re-validation BEFORE any deletion
$markerPath = Join-Path $dir ".winclipboard-install.json"
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    Write-Host "[ABORT] install marker missing; this directory is not verified as a WinClipboard install. Nothing was deleted." -ForegroundColor Red
    exit 1
}
try { $m = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json }
catch {
    Write-Host "[ABORT] install marker unreadable; refusing to delete. Nothing was deleted." -ForegroundColor Red
    exit 1
}
$here = Trim-Seps $dir
$marked = Trim-Seps ([string]$m.installPath)
if ($m.app -ne "WinClipboard" -or -not $marked -or
    -not $marked.Equals($here, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "[ABORT] install marker does not match this directory; refusing to delete. Nothing was deleted." -ForegroundColor Red
    exit 1
}
# 2) never recurse across reparse points
$rp = @(Get-ChildItem -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 })
if ($rp.Count -gt 0) {
    Write-Host "[ABORT] reparse points/junctions inside the install directory; refusing recursive delete." -ForegroundColor Red
    exit 1
}
$exe = Join-Path $dir "WinClipboard.exe"
# 3) stop only THIS install's instance (graceful first; ask unless -ForceStop)
$thisIds = @(Get-Process WinClipboard -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ((Trim-Seps $_.Path) -ieq (Trim-Seps $exe)) } |
    ForEach-Object { $_.Id })
if ($thisIds.Count -gt 0) {
    foreach ($p in @(Get-Process WinClipboard -ErrorAction SilentlyContinue)) {
        if ($thisIds -contains $p.Id) { try { $p.CloseMainWindow() | Out-Null } catch { } }
    }
    $deadline = (Get-Date).AddSeconds(4)
    do {
        Start-Sleep -Milliseconds 400
        $thisIds = @(Get-Process WinClipboard -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and ((Trim-Seps $_.Path) -ieq (Trim-Seps $exe)) } |
            ForEach-Object { $_.Id })
    } while (($thisIds.Count -gt 0) -and ((Get-Date) -lt $deadline))
    if ($thisIds.Count -gt 0) {
        if (-not $ForceStop) {
            Write-Host "[ABORT] this install's WinClipboard is still running; exit it via the tray icon and retry (or -ForceStop). Nothing was deleted." -ForegroundColor Red
            exit 1
        }
        foreach ($i in $thisIds) { try { Stop-Process -Id $i -Force -ErrorAction SilentlyContinue } catch { } }
        Start-Sleep -Milliseconds 600
    }
}
# 4) shortcuts: remove only those pointing at THIS install
$ws = New-Object -ComObject WScript.Shell
foreach ($lnk in @(
        (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\WinClipboard.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "WinClipboard.lnk"))) {
    if (Test-Path -LiteralPath $lnk -PathType Leaf) {
        try {
            $t = Trim-Seps ([string]$ws.CreateShortcut($lnk).TargetPath)
            if ($t -and $t -ieq (Trim-Seps $exe)) {
                Remove-Item -LiteralPath $lnk -Force
            }
        } catch { }
    }
}
# 5) registry entry, then the directory itself last (literal paths only)
Remove-Item -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WinClipboard$RegSuffix" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $dir -Recurse -Force
Write-Host "WinClipboard uninstalled. User data kept at %APPDATA%\WinClipboard."
'@ | Set-Content -Path $uninst -Encoding ASCII

$reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WinClipboard$RegSuffix"
New-Item -Path $reg -Force | Out-Null
$size = (Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum
$props = @{
    DisplayName     = "WinClipboard"
    DisplayVersion  = $Version
    Publisher       = "haoshangyang"
    InstallLocation = $InstallDir
    DisplayIcon     = $exe
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninst`" -RegSuffix `"$RegSuffix`""
    NoModify        = 1
    NoRepair        = 1
    EstimatedSize   = [int]($size / 1KB)
}
foreach ($k in $props.Keys) {
    New-ItemProperty -Path $reg -Name $k -Value $props[$k] -PropertyType String -Force | Out-Null
}

Write-Host ""
Write-Host "Installed WinClipboard v$Version (instance $instanceId)"
Write-Host "  App:        $exe"
Write-Host "  Uninstall:  Settings > Apps (or uninstall.ps1)"
Write-Host "  User data:  %APPDATA%\WinClipboard (kept on uninstall)"
