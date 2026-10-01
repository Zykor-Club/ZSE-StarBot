Stop-ScheduledTask -TaskName QQBot -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName QQBot
Start-Sleep -Seconds 16
(Get-ScheduledTask -TaskName QQBot).State
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, StartTime
Get-Content C:\bot\server_bot_err.log -Tail 5