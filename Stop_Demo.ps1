$ErrorActionPreference = 'Stop'
$stateFile = Join-Path $PSScriptRoot 'runs\server.json'
if (!(Test-Path -LiteralPath $stateFile)) { Write-Host 'No local demo service record.'; exit 0 }
$service = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$url = "http://127.0.0.1:$($service.port)/"
try { $health = Invoke-RestMethod -Uri ($url + 'api/health') -TimeoutSec 2 }
catch { Write-Host 'The demo service is already stopped.'; exit 0 }
if ($health.app -ne 'tangdao-bay-demo' -or $health.root -ne $PSScriptRoot) { throw 'The port belongs to another application; no action taken.' }
Invoke-RestMethod -Uri ($url + 'api/shutdown') -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
Write-Host 'Tangdao Bay demo shutdown requested.'

