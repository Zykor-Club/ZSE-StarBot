$ErrorActionPreference = 'Continue'
# .NET Runtime 环境变量
$env:DOTNET_ROOT = 'C:\dotnet'
$env:PATH = 'C:\dotnet;' + $env:PATH

Set-Location 'C:\TShockServer\server'

# 循环自重启（服务器崩溃后自动拉起）
for ($i = 0; $i -lt 1; $i++) {
    Write-Host "=== TShock 启动 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
    & 'C:\TShockServer\server\TShock.Server.exe' -lang 7 -config C:\TShockServer\server\server.properties 2>&1 | ForEach-Object { Write-Host $_ }
    Write-Host "=== TShock 退出 code=$LASTEXITCODE ==="
}