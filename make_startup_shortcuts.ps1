# Game Record Tool - Startup-folder shortcuts (fallback autostart)
# Called by setup_tasks.py. Creates (or removes) four shortcuts in the
# current user's Startup folder:
#   GameRecordTool.lnk         -> pythonw.exe run.py           (at logon)
#   GameRecordToolWatchdog.lnk -> pythonw.exe watchdog_loop.py (at logon)
# pythonw means no console window appears. ASCII only (see
# register_tasks.ps1 header for why).
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][ValidateSet('create', 'remove')][string]$Mode
)
$ErrorActionPreference = 'Stop'
try {
    $startup = [Environment]::GetFolderPath('Startup')
    $names = @('GameRecordTool', 'GameRecordToolWatchdog', 'GameRecordToolCollector', 'GameRecordToolSiteFreshness')
    if ($Mode -eq 'remove') {
        foreach ($name in $names) {
            $path = Join-Path $startup ($name + '.lnk')
            if (Test-Path $path) { Remove-Item $path -Force }
        }
        Write-Output 'SHORTCUTS_REMOVED'
        exit 0
    }
    $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $pythonw)) {
        Write-Output "PS_ERROR: pythonw not found: $pythonw"
        exit 1
    }
    $shell = New-Object -ComObject WScript.Shell

    $lnk = $shell.CreateShortcut((Join-Path $startup 'GameRecordTool.lnk'))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '"' + $Root + '\run.py"'
    $lnk.WorkingDirectory = $Root
    $lnk.WindowStyle = 7
    $lnk.Description = 'Game Record Tool - main process'
    $lnk.Save()
    Write-Output 'MAIN_SHORTCUT_CREATED'

    $lnk2 = $shell.CreateShortcut((Join-Path $startup 'GameRecordToolWatchdog.lnk'))
    $lnk2.TargetPath = $pythonw
    $lnk2.Arguments = '"' + $Root + '\watchdog_loop.py"'
    $lnk2.WorkingDirectory = $Root
    $lnk2.WindowStyle = 7
    $lnk2.Description = 'Game Record Tool - watchdog loop'
    $lnk2.Save()
    Write-Output 'WATCHDOG_SHORTCUT_CREATED'

    $lnk3 = $shell.CreateShortcut((Join-Path $startup 'GameRecordToolCollector.lnk'))
    $lnk3.TargetPath = $pythonw
    $lnk3.Arguments = '"' + $Root + '\collector\server.py"'
    $lnk3.WorkingDirectory = $Root
    $lnk3.WindowStyle = 7
    $lnk3.Description = 'Game Record Tool - catalog monitor'
    $lnk3.Save()
    Write-Output 'COLLECTOR_SHORTCUT_CREATED'

    $lnk4 = $shell.CreateShortcut((Join-Path $startup 'GameRecordToolSiteFreshness.lnk'))
    $lnk4.TargetPath = $pythonw
    $lnk4.Arguments = '"' + $Root + '\site\freshness_loop.py"'
    $lnk4.WorkingDirectory = $Root
    $lnk4.WindowStyle = 7
    $lnk4.Description = 'Game Record Tool - hourly site freshness'
    $lnk4.Save()
    Write-Output 'SITE_FRESHNESS_SHORTCUT_CREATED'
    exit 0
}
catch {
    Write-Output ('PS_ERROR: ' + $_.Exception.Message)
    exit 1
}
