# WinClipboard installer safety tests (stage 3).
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File _installer_safety_test.ps1
# All work happens in a temp tree and TEST-suffixed HKCU uninstall keys;
# no real shortcut, no default install dir, no user process is touched.
# Keep this file pure ASCII (PowerShell 5.1 reads no-BOM scripts as ANSI).

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "installer_safety.ps1")

$script:Fail = 0
$script:Pass = 0
function Check([string]$Name, [bool]$Cond, [string]$Detail = "") {
    if ($Cond) { $script:Pass++; Write-Host "PASS $Name" }
    else { $script:Fail++; Write-Host "FAIL $Name $Detail" -ForegroundColor Red }
}

function Invoke-Script([string]$Script, [string[]]$ExtraArgs, [string]$Log) {
    # Runs a PS1 in a child process; returns its exit code, output to $Log.
    $all = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Script) + $ExtraArgs
    & powershell.exe @all *> $Log
    return $LASTEXITCODE
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("wclb_inst_safety_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$regBase = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
$instScript = Join-Path $PSScriptRoot "installer.ps1"
$srcDir = Resolve-CanoPath (Join-Path $PSScriptRoot "dist\WinClipboard")
$hasDist = (Test-Path -LiteralPath (Join-Path $srcDir "WinClipboard.exe") -PathType Leaf)

try {
    # ---- path canonicalization / boundary ----
    $rel = Resolve-CanoPath (Join-Path $tmp "sub\..")
    Check "resolve.relative-normalized" ($rel -ieq (Resolve-CanoPath $tmp))
    $trimmed = Resolve-CanoPath ($tmp + "\")
    Check "resolve.trailing-trimmed" ($trimmed -ieq $tmp)
    Check "resolve.drive-root-kept" ((Resolve-CanoPath "C:\") -ieq "C:\")

    Check "within.equal" (Test-PathIsWithin "$tmp\a\b" "$tmp\a\b")
    Check "within.child" (Test-PathIsWithin "$tmp\a\b\c" "$tmp\a\b")
    Check "within.sibling-prefix-not-child" (-not (Test-PathIsWithin "$tmp\WinClipboardBackup" "$tmp\WinClipboard"))
    Check "within.case-insensitive" (Test-PathIsWithin ($tmp.ToUpper() + "\x") $tmp)

    # ---- dangerous target rejection (validation function only) ----
    $checks = @(
        @{ n = "unsafe.drive-root"; t = "C:\" },
        @{ n = "unsafe.user-profile"; t = $env:USERPROFILE },
        @{ n = "unsafe.desktop-root"; t = [Environment]::GetFolderPath('Desktop') },
        @{ n = "unsafe.documents-root"; t = [Environment]::GetFolderPath('MyDocuments') },
        @{ n = "unsafe.windows-inside"; t = (Join-Path $env:WINDIR "System32\wclbx") },
        @{ n = "unsafe.program-files"; t = (Join-Path $env:ProgramFiles "wclbx") }
    )
    foreach ($c in $checks) {
        $r = Get-UnsafeTargetReason $c.t $srcDir $PSScriptRoot
        Check $c.n ($null -ne $r) "reason=$r"
    }
    Check "unsafe.project-dir" ($null -ne (Get-UnsafeTargetReason $PSScriptRoot $srcDir $PSScriptRoot))
    Check "unsafe.project-ancestor" ($null -ne (Get-UnsafeTargetReason (Split-Path $PSScriptRoot -Parent) $srcDir $PSScriptRoot))
    Check "unsafe.source-ancestor" ($null -ne (Get-UnsafeTargetReason (Split-Path $srcDir -Parent) $srcDir $PSScriptRoot))
    Check "unsafe.inside-source" ($null -ne (Get-UnsafeTargetReason (Join-Path $srcDir "sub") $srcDir $PSScriptRoot))
    $safeT = Join-Path $tmp "fresh\wclb"
    $r = Get-UnsafeTargetReason $safeT $srcDir $PSScriptRoot
    Check "safe.fresh-subdir" ($null -eq $r) "reason=$r"

    # ---- install marker ----
    $md = Join-Path $tmp "owned"
    New-Item -ItemType Directory -Force -Path $md | Out-Null
    $iid = New-InstallMarker $md "9.9.9" ""
    Check "marker.created-valid" (Test-InstallMarkerValid $md)
    Check "marker.instance-guid" ($iid -match '^[0-9a-f\-]{36}$')
    $iid2 = New-InstallMarker $md "9.9.10" $iid
    Check "marker.instance-stable" ($iid2 -eq $iid)
    $moved = Join-Path $tmp "owned-moved"
    Copy-Item -LiteralPath $md -Destination $moved -Recurse -Force
    Check "marker.path-mismatch-invalid" (-not (Test-InstallMarkerValid $moved))
    $mfile = Join-Path $md ".winclipboard-install.json"
    $tampered = Get-Content -LiteralPath $mfile -Raw | ConvertFrom-Json
    $tampered.app = "OtherApp"
    $tampered | ConvertTo-Json | Set-Content -LiteralPath $mfile -Encoding ASCII
    Check "marker.wrong-app-invalid" (-not (Test-InstallMarkerValid $md))
    New-InstallMarker $md "9.9.9" "" | Out-Null
    $json = Get-Content -LiteralPath $mfile -Raw | ConvertFrom-Json
    $json.instanceId = ""
    $json | ConvertTo-Json | Set-Content -LiteralPath $mfile -Encoding ASCII
    Check "marker.no-instance-invalid" (-not (Test-InstallMarkerValid $md))
    $plain = Join-Path $tmp "plain"
    New-Item -ItemType Directory -Force -Path $plain | Out-Null
    Set-Content -LiteralPath (Join-Path $plain "sentinel.txt") -Value "keep"
    Check "marker.nonempty-no-marker-invalid" (-not (Test-InstallMarkerValid $plain))

    # ---- reparse points ----
    $jt = Join-Path $tmp "with-junction"
    New-Item -ItemType Directory -Force -Path $jt | Out-Null
    $juncMade = $true
    try {
        New-Item -ItemType Junction -Path (Join-Path $jt "j") -Target $tmp -ErrorAction Stop | Out-Null
    } catch { $juncMade = $false }
    if ($juncMade) {
        Check "reparse.detected" ((Find-ReparsePoints $jt).Count -eq 1)
    } else {
        Write-Host "SKIP reparse.detected (junction creation unavailable)"
    }
    Check "reparse.none-in-clean" ((Find-ReparsePoints $md).Count -eq 0)

    # ---- process matching (stand-in process objects) ----
    $exeA = Join-Path $tmp "WinClipboard.exe"
    $procs = @(
        (New-Object PSObject -Property @{ Id = 11; Path = $exeA }),
        (New-Object PSObject -Property @{ Id = 12; Path = "D:\other\WinClipboard.exe" }),
        (New-Object PSObject -Property @{ Id = 13; Path = $null })
    )
    $ids = Get-TargetProcessIds $procs $exeA
    Check "process.match-by-path" ($ids.Count -eq 1 -and $ids[0] -eq 11)
    Check "process.case-insensitive" ((Get-TargetProcessIds $procs ($exeA.ToUpper())).Count -eq 1)

    # ---- shortcut target check ----
    $dummyExe = Join-Path $tmp "dummy.exe"
    Set-Content -LiteralPath $dummyExe -Value "x"
    $ws = New-Object -ComObject WScript.Shell
    $lnkPath = Join-Path $tmp "t.lnk"
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = $dummyExe
    $lnk.Save()
    Check "shortcut.points-to-true" (Test-ShortcutPointsTo $lnkPath $dummyExe)
    Check "shortcut.points-to-false" (-not (Test-ShortcutPointsTo $lnkPath $exeA))

    # ---- full installer flow (needs a real dist build; isolated target) ----
    if (-not $hasDist) {
        Write-Host "SKIP full-flow tests (dist\WinClipboard\WinClipboard.exe missing)"
    } else {
        $inst = Join-Path $tmp "inst"
        $code = Invoke-Script $instScript @("-InstallDir", $inst, "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST") "$tmp\install1.log"
        Check "install.exit0" ($code -eq 0) ((Get-Content "$tmp\install1.log" -Tail 3 | Out-String))
        Check "install.marker-written" (Test-InstallMarkerValid $inst)
        $reg1 = Join-Path $regBase "WinClipboardWCLBTEST"
        Check "install.registry-created" (Test-Path $reg1)
        $m1 = Read-InstallMarker $inst
        # update path: non-empty dir with valid marker is allowed; instance id stable
        $code = Invoke-Script $instScript @("-InstallDir", $inst, "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST") "$tmp\install2.log"
        Check "install.update-allowed" ($code -eq 0)
        $m2 = Read-InstallMarker $inst
        Check "install.instance-stable" ($m1.instanceId -eq $m2.instanceId)

        # version mismatch: aborts BEFORE any mutation (no registry key)
        $code = Invoke-Script $instScript @("-InstallDir", (Join-Path $tmp "inst2"), "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST2", "-Version", "0.0.0-mismatch") "$tmp\install3.log"
        Check "install.version-mismatch-aborts" ($code -ne 0)
        Check "install.mismatch-no-registry" (-not (Test-Path (Join-Path $regBase "WinClipboardWCLBTEST2")))

        # unowned non-empty dir: refused, sentinel intact, nothing registered
        $code = Invoke-Script $instScript @("-InstallDir", $plain, "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST3") "$tmp\install4.log"
        Check "install.unowned-dir-refused" ($code -ne 0)
        Check "install.sentinel-kept" ((Get-Content -LiteralPath (Join-Path $plain "sentinel.txt") -Raw).Trim() -eq "keep")
        Check "install.unowned-no-registry" (-not (Test-Path (Join-Path $regBase "WinClipboardWCLBTEST3")))

        # uninstaller: ownership revalidation + self cleanup
        $uninst = Join-Path $inst "uninstall.ps1"
        $code = Invoke-Script $uninst @("-RegSuffix", "WCLBTEST") "$tmp\uninst1.log"
        Check "uninstall.exit0" ($code -eq 0) ((Get-Content "$tmp\uninst1.log" -Tail 3 | Out-String))
        Check "uninstall.dir-removed" (-not (Test-Path -LiteralPath $inst))
        Check "uninstall.registry-removed" (-not (Test-Path (Join-Path $regBase "WinClipboardWCLBTEST")))

        # uninstaller without a marker: aborts, deletes nothing
        $stub = Join-Path $tmp "stub-uninst"
        New-Item -ItemType Directory -Force -Path $stub | Out-Null
        Copy-Item -LiteralPath (Join-Path $srcDir "..\installer.ps1") -Destination $stub -ErrorAction SilentlyContinue
        # generate a real uninstaller via a throwaway install, then move it
        $throwaway = Join-Path $tmp "throwaway"
        $null = Invoke-Script $instScript @("-InstallDir", $throwaway, "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST5") "$tmp\install7.log"
        if (Test-Path (Join-Path $throwaway "uninstall.ps1")) {
            Copy-Item -LiteralPath (Join-Path $throwaway "uninstall.ps1") -Destination (Join-Path $stub "uninstall.ps1")
            Set-Content -LiteralPath (Join-Path $stub "sentinel2.txt") -Value "keep2"
            $code = Invoke-Script (Join-Path $stub "uninstall.ps1") @() "$tmp\uninst2.log"
            Check "uninstall.no-marker-aborts" ($code -ne 0)
            Check "uninstall.no-marker-keeps-files" (Test-Path (Join-Path $stub "sentinel2.txt"))
            Remove-Item -LiteralPath $stub -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath (Join-Path $regBase "WinClipboardWCLBTEST5") -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path $throwaway) {
                Remove-Item -LiteralPath $throwaway -Recurse -Force -ErrorAction SilentlyContinue
            }
        } else {
            Write-Host "SKIP uninstall.no-marker-aborts (uninstaller generation unavailable)"
        }

        # copy failure: lock the installed EXE (exists in src too, so /MIR must
        # overwrite it) -> robocopy fails with code >= 8 -> abort, no registry
        $locked = Join-Path $tmp "lockedinst"
        $code = Invoke-Script $instScript @("-InstallDir", $locked, "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST4") "$tmp\install5.log"
        if ($code -eq 0) {
            $lockedExe = Join-Path $locked "WinClipboard.exe"
            (Get-Item -LiteralPath $lockedExe).LastWriteTime = (Get-Date).AddDays(-30)
            $fs = [System.IO.File]::Open($lockedExe,
                [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::None)
            try {
                $code2 = Invoke-Script $instScript @("-InstallDir", $locked, "-NoDesktop", "-NoStartMenu", "-RegSuffix", "WCLBTEST4B") "$tmp\install6.log"
                $regThere = Test-Path (Join-Path $regBase "WinClipboardWCLBTEST4B")
                Check "install.copy-fail-no-registry" (($code2 -ne 0) -and (-not $regThere)) "exit=$code2 reg=$regThere log=$((Get-Content "$tmp\install6.log" -Tail 2 | Out-String))"
            } finally {
                $fs.Close()
            }
        } else {
            Write-Host "SKIP locked-file case (first install into locked dir failed unexpectedly)"
        }
        foreach ($sfx in @("WCLBTEST", "WCLBTEST2", "WCLBTEST3", "WCLBTEST4", "WCLBTEST4B", "WCLBTEST5")) {
            Remove-Item -LiteralPath (Join-Path $regBase "WinClipboard$sfx") -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Fail -eq 0) {
    Write-Host ("RESULT: PASS (" + $script:Pass + " checks)")
    exit 0
} else {
    Write-Host ("RESULT: FAILED (" + $script:Fail + " failed, " + $script:Pass + " passed)") 
    exit 1
}
