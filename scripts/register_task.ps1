$ErrorActionPreference = "Stop"
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$pushScript = Join-Path $PSScriptRoot "push_usage.ps1"
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Python "{1}"' -f $pushScript, $pythonPath
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At "21:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive
Register-ScheduledTask -TaskName "LLRHook usage card" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
