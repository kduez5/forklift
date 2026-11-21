param([ValidateRange(1024,65535)][int]$Port = 8765)
$packageParent = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $packageParent
try { python -m manual_station --backend mock --port $Port }
finally { Pop-Location }
