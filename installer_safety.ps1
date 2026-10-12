# WinClipboard installer safety helpers (pure functions, no side effects).
# Dot-sourced by installer.ps1 and _installer_safety_test.ps1.
# Keep this file pure ASCII (PowerShell 5.1 reads no-BOM scripts as ANSI).

$script:WclbMarkerName = ".winclipboard-install.json"
$script:WclbAppId = "WinClipboard"

function Resolve-CanoPath([string]$Path) {
    <# Canonical absolute path: expands env vars, resolves . / ..,
    trims trailing separators (drive roots keep their form). $null on error. #>
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    try { $full = [System.IO.Path]::GetFullPath($expanded) } catch { return $null }
    while ($full.Length -gt 3 -and $full.EndsWith('\')) {
        $full = $full.Substring(0, $full.Length - 1)
    }
    return $full
}

function Test-PathIsWithin([string]$Child, [string]$Parent) {
    <# True when Child equals Parent or lies inside it. Boundary-aware and
    case-insensitive: "C:\X\WinClipboardB" is NOT within "C:\X\WinClipboard". #>
    if ([string]::IsNullOrWhiteSpace($Child) -or [string]::IsNullOrWhiteSpace($Parent)) {
        return $false
    }
    $c = Resolve-CanoPath $Child
    $p = Resolve-CanoPath $Parent
    if ($null -eq $c -or $null -eq $p) { return $false }
    if ($p.Length -eq 3) {
        return $c.StartsWith($p.Substring(0, 2), [StringComparison]::OrdinalIgnoreCase)
    }
    return ($c -ieq $p) -or
        $c.StartsWith($p + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-UnsafeTargetReason([string]$Target, [string]$SrcDir, [string]$ProjectDir) {
    <# Returns a reason string when the mirror/uninstall target is a location
    we refuse to touch; $null when acceptable from a path-safety standpoint. #>
    $t = Resolve-CanoPath $Target
    if ($null -eq $t) { return "target path cannot be resolved: $Target" }
    if ($t.Length -eq 3) { return "refusing drive root: $t" }
    $downloads = $null
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $downloads = Join-Path $env:USERPROFILE 'Downloads'
    }
    $roots = @(
        $env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, $env:ProgramData,
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('MyDocuments'),
        [Environment]::GetFolderPath('MyPictures'),
        $downloads,
        $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramW6432,
        $env:WINDIR
    )
    foreach ($r in $roots) {
        if (-not [string]::IsNullOrWhiteSpace($r)) {
            $cr = Resolve-CanoPath $r
            if ($cr -and ($t -ieq $cr)) { return "refusing protected folder root: $r" }
        }
    }
    if ($env:WINDIR -and (Test-PathIsWithin $t $env:WINDIR)) {
        return "refusing location inside the Windows directory"
    }
    foreach ($pf in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramW6432)) {
        if (-not [string]::IsNullOrWhiteSpace($pf) -and (Test-PathIsWithin $t $pf)) {
            return "refusing location inside Program Files: $pf"
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($SrcDir)) {
        $s = Resolve-CanoPath $SrcDir
        if ($s) {
            if ($t -ieq $s) { return "target equals the build source directory" }
            if (Test-PathIsWithin $s $t) {
                return "target is an ancestor of the build source (mirror would copy into itself)"
            }
            if (Test-PathIsWithin $t $s) { return "target is inside the build source directory" }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($ProjectDir)) {
        $pj = Resolve-CanoPath $ProjectDir
        if ($pj) {
            if ($t -ieq $pj) { return "target equals the project directory" }
            if (Test-PathIsWithin $pj $t) {
                return "target is an ancestor of the project directory"
            }
        }
    }
    return $null
}

function Read-InstallMarker([string]$Dir) {
    <# Parsed marker hashtable or $null (missing/unreadable). #>
    if ([string]::IsNullOrWhiteSpace($Dir)) { return $null }
    $m = Join-Path $Dir $script:WclbMarkerName
    if (-not (Test-Path -LiteralPath $m -PathType Leaf)) { return $null }
    try {
        return (Get-Content -LiteralPath $m -Raw | ConvertFrom-Json)
    } catch { return $null }
}

function Test-InstallMarkerValid([string]$Dir) {
    <# True only when the marker exists, names this app, has an instance id
    and records exactly this (canonical) directory. #>
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { return $false }
    $m = Read-InstallMarker $Dir
    if ($null -eq $m) { return $false }
    if ($m.app -ne $script:WclbAppId) { return $false }
    if ([string]::IsNullOrWhiteSpace([string]$m.instanceId)) { return $false }
    $canon = Resolve-CanoPath $Dir
    $marked = Resolve-CanoPath ([string]$m.installPath)
    if ($null -eq $canon -or $null -eq $marked) { return $false }
    return ($marked -ieq $canon)
}

function New-InstallMarker([string]$Dir, [string]$Version, [string]$InstanceId) {
    <# Writes the ownership marker; keeps a stable instance id across updates
    when one is supplied. #>
    if ([string]::IsNullOrWhiteSpace($InstanceId)) {
        $InstanceId = [guid]::NewGuid().ToString()
    }
    $m = @{
        app         = $script:WclbAppId
        installPath = (Resolve-CanoPath $Dir)
        instanceId  = $InstanceId
        version     = $Version
        created     = (Get-Date).ToUniversalTime().ToString("o")
    }
    $m | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Dir $script:WclbMarkerName) -Encoding ASCII
    return $InstanceId
}

function Find-ReparsePoints([string]$Dir) {
    <# Reparse points (junctions/symlinks) anywhere under Dir. #>
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { return @() }
    $found = Get-ChildItem -LiteralPath $Dir -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 }
    return @($found)
}

function Get-TargetProcessIds([object[]]$Processes, [string]$ExePath) {
    <# Ids of processes whose executable path equals ExePath (canonical,
    case-insensitive). Takes process objects so tests can pass stand-ins and
    same-named processes from other installs are never matched. #>
    $exe = Resolve-CanoPath $ExePath
    if ($null -eq $exe -or $null -eq $Processes) { return @() }
    $out = @()
    foreach ($p in $Processes) {
        if ($p -and $p.Path) {
            $pp = Resolve-CanoPath ([string]$p.Path)
            if ($pp -and ($pp -ieq $exe)) { $out += $p.Id }
        }
    }
    return $out
}

function Test-ShortcutPointsTo([string]$ShortcutPath, [string]$ExePath) {
    <# True when the .lnk exists and its target equals ExePath. #>
    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) { return $false }
    try {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($ShortcutPath)
        $t = Resolve-CanoPath ([string]$lnk.TargetPath)
        $e = Resolve-CanoPath $ExePath
        return ($t -and $e -and ($t -ieq $e))
    } catch { return $false }
}
