param([switch]$NoBrowser, [switch]$PrepareOnly, [switch]$PortableOnly)
$ErrorActionPreference = 'Stop'
$demoRoot = $PSScriptRoot
$runDir = Join-Path $demoRoot 'runs'
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$serverFile = Join-Path $demoRoot 'server.py'
if (!(Test-Path -LiteralPath $serverFile)) { throw "Missing demo server: $serverFile" }

Write-Host 'Tangdao Bay 3D demo - checking local runtime ...'
Write-Host 'On first launch, missing dependencies are downloaded from their official HTTPS sites.'
$runtime = & (Join-Path $demoRoot 'Setup_Runtime.ps1') -PortableOnly:$PortableOnly
$pythonExe = $runtime.python
$sumoRoot = $runtime.sumo
$runtime | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir 'runtime.json') -Encoding UTF8
$env:SUMO_HOME = $sumoRoot
$env:PATH = (Join-Path $sumoRoot 'bin') + ';' + $env:PATH
$env:PYTHONPATH = Join-Path $sumoRoot 'tools'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if ($PrepareOnly) { Write-Host 'Runtime ready. No server or browser was started.'; return }

$port = $null
$reuse = $false
foreach ($candidatePort in 8765..8775) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$candidatePort/api/health" -TimeoutSec 1
        if ($health.app -eq 'tangdao-bay-demo' -and $health.root -eq $demoRoot) {
            $port = $candidatePort
            $reuse = $true
            break
        }
    } catch { }
    $probe = $null
    try {
        $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidatePort)
        $probe.Start()
        $port = $candidatePort
        break
    } catch { }
    finally { if ($probe) { $probe.Stop() } }
}
if (!$port) { throw 'No free local demo port in 8765-8775.' }
$url = "http://127.0.0.1:$port/"
if (!$reuse) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
    $stdout = Join-Path $runDir "server_$stamp.stdout.log"
    $stderr = Join-Path $runDir "server_$stamp.stderr.log"
    $serverArgs = @('-B', '-u', ('"' + $serverFile + '"'), '--host', '127.0.0.1', '--port', $port)
    $demoProcess = Start-Process -FilePath $pythonExe -ArgumentList $serverArgs -WorkingDirectory $demoRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        Start-Sleep -Milliseconds 250
        $demoProcess.Refresh()
        if ($demoProcess.HasExited) { throw "Demo server exited. Read $stderr" }
        try {
            $health = Invoke-RestMethod -Uri ($url + 'api/health') -TimeoutSec 1
            if ($health.app -eq 'tangdao-bay-demo' -and $health.root -eq $demoRoot -and $health.ready) { $ready = $true; break }
        } catch { }
    }
    if (!$ready) { throw "Server did not become ready. Read $stderr" }
    [pscustomobject]@{pid=$demoProcess.Id;port=$port;root=$demoRoot;started=(Get-Date).ToString('o');python=$pythonExe;stdout=$stdout;stderr=$stderr} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir 'server.json') -Encoding UTF8
}
Write-Host "Tangdao Bay 3D demo: $url"
Write-Host 'SUMO supplies live traffic. Acoustic events and RSU infrastructure are simulated.'
Write-Host 'Use Stop_Demo.cmd to stop this local service.'
Write-Host 'The simulation starts PAUSED. Click Play / Continue in the web page.'
if (!$NoBrowser) { Start-Process $url }
