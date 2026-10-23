# Installs the Daily Brief launcher on Windows: desktop shortcut, the daily
# 08:00 task, and a logon catch-up. Re-run after moving the folder.
#
# This file is deliberately pure ASCII: Windows PowerShell reads a BOM-less
# script as ANSI, so a non-ASCII literal here would corrupt the parse. All
# localized text lives in locales/<lang>.json and is read back as UTF-8.
$ErrorActionPreference = 'Stop'

$Root     = $PSScriptRoot
$Vbs      = Join-Path $Root 'Launch Daily Brief.vbs'
$Icon     = Join-Path $Root 'brief.ico'
$Digest   = Join-Path $Root 'digest.py'
$ConfigF  = Join-Path $Root 'config.json'
$TaskName = 'Daily Brief'
$Desktop  = [Environment]::GetFolderPath('Desktop')
$Startup  = [Environment]::GetFolderPath('Startup')

# ---------------------------------------------------------------- config
$lang = 'en'
if (Test-Path $ConfigF) {
    try {
        $cfg = Get-Content -Raw -Encoding UTF8 $ConfigF | ConvertFrom-Json
        if ($cfg.lang) { $lang = $cfg.lang }
    } catch {
        Write-Host 'warning: could not parse config.json, using lang=en'
    }
}
$localeFile = Join-Path (Join-Path $Root 'locales') ($lang + '.json')
if (-not (Test-Path $localeFile)) { $localeFile = Join-Path (Join-Path $Root 'locales') 'en.json' }
$S = Get-Content -Raw -Encoding UTF8 $localeFile | ConvertFrom-Json
Write-Host "lang    : $lang"

# ---------------------------------------------------------------- python
# Find pythonw.exe without assuming a particular 3.x version or install layout.
$candidates = @()
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    try {
        $exe = (& py -3 -c "import sys;print(sys.executable)") 2>$null
        if ($exe -and (Test-Path $exe)) {
            $candidates += (Join-Path (Split-Path $exe) 'pythonw.exe')
        }
    } catch { }
}
$onPath = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }
foreach ($v in @('314', '313', '312', '311', '310', '39')) {
    $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\pythonw.exe")
    $candidates += (Join-Path $env:ProgramFiles "Python$v\pythonw.exe")
}
$pythonw = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $pythonw) {
    Write-Host 'error: could not find pythonw.exe.' -ForegroundColor Red
    Write-Host '       Install Python 3 from python.org, or edit install.ps1 to point at yours.'
    exit 1
}
Write-Host "pythonw : $pythonw"

# ---------------------------------------------------------------- shortcut
$wsh = New-Object -ComObject WScript.Shell
$shortcut = Join-Path $Desktop ($S.shortcut_name + '.lnk')
$lnk = $wsh.CreateShortcut($shortcut)
$lnk.TargetPath       = Join-Path $env:SystemRoot 'System32\wscript.exe'
$lnk.Arguments        = '"' + $Vbs + '"'
$lnk.WorkingDirectory = $Root
$lnk.Description      = $S.shortcut_desc
if (Test-Path $Icon) { $lnk.IconLocation = ($Icon + ',0') }
$lnk.Save()
Write-Host "shortcut: $shortcut"

# ------------------------------------------------------- daily 08:00 task
# StartWhenAvailable alone is not enough: if the PC is off at 08:00 the run is
# recorded as "missed" and skipped to the next day. Changing an existing task
# can require elevation, so failure here is tolerated -- the logon catch-up
# below covers that case.
$arguments = '"' + $Digest + '" --quiet'
try {
    $action   = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Root
    $trigger  = New-ScheduledTaskTrigger -Daily -At 08:00
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force -Description $S.task_desc -ErrorAction Stop | Out-Null
    Write-Host "task    : '$TaskName' registered (daily 08:00)"
} catch {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "task    : could not modify '$TaskName' ($($_.Exception.Message.Trim()))"
        Write-Host '          keeping the existing task; run this from an elevated shell to change it'
    } else {
        Write-Host "task    : could not create '$TaskName' ($($_.Exception.Message.Trim()))"
        Write-Host '          run this from an elevated PowerShell to install it'
    }
}

# ------------------------------------------------- logon catch-up (no admin)
$catchup = Join-Path $Startup ($S.shortcut_name + ' (catch-up).lnk')
$cu = $wsh.CreateShortcut($catchup)
$cu.TargetPath       = $pythonw
$cu.Arguments        = '"' + $Digest + '" --quiet --if-stale'
$cu.WorkingDirectory = $Root
$cu.Description      = $S.startup_desc
if (Test-Path $Icon) { $cu.IconLocation = ($Icon + ',0') }
$cu.Save()
Write-Host "catchup : $catchup"

# ---------------------------------------------------------------- verify
Write-Host ''
Write-Host '--- verify ---'
if (Test-Path $shortcut) { Write-Host 'OK  desktop shortcut' } else { Write-Host 'FAIL desktop shortcut missing' }
if (Test-Path $catchup)  { Write-Host 'OK  logon catch-up' }  else { Write-Host 'FAIL logon catch-up missing' }
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($t) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("OK  task state={0} next={1}" -f $t.State, $info.NextRunTime)
} else {
    Write-Host 'FAIL task not found'
}
