# Game Record Tool - Task Scheduler registration (PowerShell route)
# Called by setup_tasks.py when the classic schtasks route fails.
# Registers all tasks for the CURRENT user:
#   GameRecordTool         - at logon, runs run.py
#   GameRecordToolWatchdog - every 15 minutes, runs watchdog.py
#   GameRecordToolCollector - at logon, runs the local collector server
#   GameRecordToolSiteFreshness - hourly deterministic site refresh
# Two attempts per task: first with an explicit principal
# (DOMAIN\user, Interactive, Limited), then - if that is rejected
# (seen on some Microsoft-account / home PCs) - a bare registration
# that lets Task Scheduler default the principal to the current
# user. ASCII only on purpose: Windows PowerShell 5.1 reads .ps1
# files without a BOM as ANSI, so non-ASCII text would corrupt.
param(
    [Parameter(Mandatory = $true)][string]$Root
)
$ErrorActionPreference = 'Stop'

function Register-GrtTask {
    param([string]$Name, $Action, $Trigger, $Settings, $Principal)
    try {
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
        Write-Output "REGISTERED_WITH_PRINCIPAL: $Name"
    }
    catch {
        Write-Output ("PRINCIPAL_ATTEMPT_FAILED: $Name : " + $_.Exception.Message)
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
        Write-Output "REGISTERED_WITH_DEFAULT_PRINCIPAL: $Name"
    }
}

try {
    $python = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $python)) {
        Write-Output "PS_ERROR: venv python not found: $python"
        exit 1
    }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

    $mainAction = New-ScheduledTaskAction -Execute $python -Argument ('"' + $Root + '\run.py"') -WorkingDirectory $Root
    $mainTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    Register-GrtTask -Name 'GameRecordTool' -Action $mainAction -Trigger $mainTrigger -Settings $settings -Principal $principal

    $collectorAction = New-ScheduledTaskAction -Execute $python -Argument ('"' + $Root + '\collector\server.py"') -WorkingDirectory $Root
    Register-GrtTask -Name 'GameRecordToolCollector' -Action $collectorAction -Trigger $mainTrigger -Settings $settings -Principal $principal

    $wdAction = New-ScheduledTaskAction -Execute $python -Argument ('"' + $Root + '\watchdog.py"') -WorkingDirectory $Root
    $wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 36500)
    Register-GrtTask -Name 'GameRecordToolWatchdog' -Action $wdAction -Trigger $wdTrigger -Settings $settings -Principal $principal

    $siteAction = New-ScheduledTaskAction -Execute $python -Argument ('"' + $Root + '\site\update_site.py"') -WorkingDirectory $Root
    $siteTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 36500)
    Register-GrtTask -Name 'GameRecordToolSiteFreshness' -Action $siteAction -Trigger $siteTrigger -Settings $settings -Principal $principal
    exit 0
}
catch {
    Write-Output ('PS_ERROR: ' + $_.Exception.Message)
    exit 1
}
