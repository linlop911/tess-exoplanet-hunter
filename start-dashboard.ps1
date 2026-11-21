$ErrorActionPreference = 'Stop'
$Port = 18805
$Url = "http://127.0.0.1:$Port/"
$Script = Join-Path $PSScriptRoot 'web\server.py'

$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Start-Process -FilePath 'py' -ArgumentList @('-3.11', $Script) -WindowStyle Hidden
    Start-Sleep -Seconds 1
}
Start-Process $Url
Write-Host "TESS Observatory Dashboard ready at $Url"
