$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\bot\start_bot.ps1' -WorkingDirectory 'C:\bot'
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName 'QQBot' -Action $action -Trigger $trigger -RunLevel Highest -Force | Out-Null
(Get-ScheduledTask -TaskName 'QQBot') | Select-Object TaskName, State