# 开机自启启动脚本：后台运行机器人
cd C:\bot
Start-Process -FilePath 'C:\Program Files\Python313\python.exe' -ArgumentList 'C:\bot\main.py' -WorkingDirectory 'C:\bot' -WindowStyle Hidden -RedirectStandardOutput 'C:\bot\server_bot.log' -RedirectStandardError 'C:\bot\server_bot_err.log'