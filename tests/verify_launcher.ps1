param([switch]$SkipDownloadExercise)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$results = New-Object 'System.Collections.Generic.List[object]'
$manifest = Get-Content -LiteralPath (Join-Path $project 'runtime-manifest.json') -Raw | ConvertFrom-Json
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('TangdaoLauncherValidation_' + [guid]::NewGuid().ToString('N'))
$relocated = Join-Path $testRoot ([string]([char]0x5510) + [char]0x5C9B + ' Bay portable')
$originalHealth = $null
try { $originalHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2 } catch { }

function Check([string]$Name, [bool]$Condition, $Details = $null) {
    if (!$Condition) { throw "FAILED: $Name" }
    $results.Add([pscustomobject]@{check=$Name;passed=$true;details=$Details})
    Write-Host "PASS: $Name"
}
function Launch([string]$Folder, [switch]$Prepare) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Folder 'Start_Demo.ps1'), '-PortableOnly', '-NoBrowser')
    if ($Prepare) { $arguments += '-PrepareOnly' }
    & powershell.exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Launcher exited with $LASTEXITCODE" }
}

try {
    foreach ($name in @('Start_Demo.ps1', 'Setup_Runtime.ps1', 'Stop_Demo.ps1')) {
        $tokens = $null; $parseErrors = $null
        [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $project $name), [ref]$tokens, [ref]$parseErrors)
        Check "PowerShell syntax: $name" ($parseErrors.Count -eq 0)
    }
    foreach ($name in @('python', 'sumo')) {
        $dependency = $manifest.$name
        $cacheFile = Join-Path $project ('_runtime\downloads\' + $dependency.archive)
        if (Test-Path -LiteralPath $cacheFile) {
            Check "$name official archive SHA256" ((Get-FileHash -Algorithm SHA256 -LiteralPath $cacheFile).Hash -eq $dependency.sha256) $dependency.sha256
        }
    }
    New-Item -ItemType Directory -Path $relocated -Force | Out-Null
    foreach ($name in @('Start_Demo.ps1', 'Setup_Runtime.ps1', 'Stop_Demo.ps1', 'runtime-manifest.json', 'server.py', 'simulation.py', 'scenario', 'data', 'web')) {
        Copy-Item -LiteralPath (Join-Path $project $name) -Destination $relocated -Recurse
    }
    New-Item -ItemType Directory -Path (Join-Path $relocated '_runtime') -Force | Out-Null
    foreach ($dependency in @($manifest.python, $manifest.sumo)) {
        Copy-Item -LiteralPath (Join-Path $project ('_runtime\' + $dependency.directory)) -Destination (Join-Path $relocated '_runtime') -Recurse
    }
    Launch $relocated -Prepare
    Check 'PrepareOnly starts no server' (!(Test-Path -LiteralPath (Join-Path $relocated 'runs\server.json')))
    Check 'Offline relocation needs no downloaded archive cache' (!(Test-Path -LiteralPath (Join-Path $relocated '_runtime\downloads')))
    $runtime = Get-Content -LiteralPath (Join-Path $relocated 'runs\runtime.json') -Raw | ConvertFrom-Json
    Check 'PortableOnly ignores system Python and SUMO' ($runtime.portablePython -and $runtime.portableSumo)
    $pth = Get-Content -LiteralPath (Join-Path $relocated ('_runtime\' + $manifest.python.directory + '\python312._pth'))
    Check 'Embedded Python paths are relative' (($pth -join '|') -eq 'python312.zip|.|..\..')
    Check 'Python and SUMO licenses retained' ((Test-Path -LiteralPath (Join-Path $relocated ('_runtime\' + $manifest.python.directory + '\LICENSE.txt'))) -and
        (Test-Path -LiteralPath (Join-Path $relocated ('_runtime\' + $manifest.sumo.directory + '\LICENSE'))) -and
        (Test-Path -LiteralPath (Join-Path $relocated ('_runtime\' + $manifest.sumo.directory + '\NOTICE.md'))) -and
        (Test-Path -LiteralPath (Join-Path $relocated ('_runtime\' + $manifest.sumo.directory + '\docs\userdoc\Libraries_Licenses.html'))))
    Launch $relocated
    $service = Get-Content -LiteralPath (Join-Path $relocated 'runs\server.json') -Raw | ConvertFrom-Json
    $base = "http://127.0.0.1:$($service.port)"
    $health = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 3
    Check 'Relocated Unicode and spaced path starts real SUMO' ($health.ready -and $health.backend -eq 'SUMO/TraCI' -and $health.root -eq $relocated) @{sumo=$health.sumo;port=$service.port}
    Check 'Initial state is paused' ($health.status -eq 'paused')
    Launch $relocated
    $same = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 3
    Check 'Repeated launch reuses this project service' ($same.pid -eq $health.pid)
    Invoke-RestMethod -Uri "$base/api/control" -Method Post -ContentType 'application/json' -Body '{"action":"speed","value":4}' | Out-Null
    Invoke-RestMethod -Uri "$base/api/control" -Method Post -ContentType 'application/json' -Body '{"action":"resume"}' | Out-Null
    Start-Sleep -Seconds 3
    Invoke-RestMethod -Uri "$base/api/control" -Method Post -ContentType 'application/json' -Body '{"action":"pause"}' | Out-Null
    $state = Invoke-RestMethod -Uri "$base/api/state" -TimeoutSec 3
    Check 'Portable SUMO advances traffic after resume' ($state.simTime -gt 0 -and $state.vehicles.Count -gt 0) @{simTime=$state.simTime;vehicles=$state.vehicles.Count}
    Start-Sleep -Milliseconds 400
    $paused = Invoke-RestMethod -Uri "$base/api/state" -TimeoutSec 3
    Check 'Pause stops the simulation clock' ($paused.simTime -eq $state.simTime)
    & (Join-Path $relocated 'Stop_Demo.ps1')
    Start-Sleep -Milliseconds 800
    $stopped = $false
    try { Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 2 | Out-Null } catch { $stopped = $true }
    Check 'Stop_Demo gracefully stops only relocated service' $stopped

    if (!$SkipDownloadExercise) {
        # Load the exact checked-in downloader function and test it against the official Python archive.
        $tokens = $null; $parseErrors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $project 'Setup_Runtime.ps1'), [ref]$tokens, [ref]$parseErrors)
        $functionAst = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedArchive' }, $true)
        Invoke-Expression $functionAst.Extent.Text
        $runtimeRoot = Join-Path $testRoot 'download-test'
        $downloaded = Get-VerifiedArchive $manifest.python
        Check 'Official HTTPS downloader and SHA256 check' ((Get-FileHash -LiteralPath $downloaded -Algorithm SHA256).Hash -eq $manifest.python.sha256)
        $stamp = (Get-Item -LiteralPath $downloaded).LastWriteTimeUtc
        [void](Get-VerifiedArchive $manifest.python)
        Check 'Verified download cache is reused' ((Get-Item -LiteralPath $downloaded).LastWriteTimeUtc -eq $stamp)
        [IO.File]::WriteAllText($downloaded, 'corrupted test archive')
        $rejected = $false
        try { [void](Get-VerifiedArchive $manifest.python) } catch { $rejected = $_.Exception.Message -like '*failed SHA256*' }
        Check 'Corrupted runtime archive is rejected before extraction' $rejected
    }
    if ($originalHealth) {
        $afterOriginal = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 3
        Check 'Original port 8765 service was not replaced or stopped' ($afterOriginal.pid -eq $originalHealth.pid -and $afterOriginal.root -eq $originalHealth.root)
    }
    $evidence = Join-Path $project 'evidence\delivery'
    New-Item -ItemType Directory -Path $evidence -Force | Out-Null
    [pscustomobject]@{
        schemaVersion=1;created=(Get-Date).ToString('o');passed=$true;checks=$results
        scope='Windows PowerShell launcher, official runtime SHA256, offline relocation to a Unicode/spaced temporary path, real SUMO/TraCI start/pause/resume/stop. Not a fresh Windows virtual machine or browser GPU test.'
        python=$manifest.python.version;sumo=$manifest.sumo.version;downloadExercise=(!$SkipDownloadExercise)
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $evidence 'launcher_validation.json') -Encoding UTF8
} finally {
    if (Test-Path -LiteralPath (Join-Path $relocated 'runs\server.json')) {
        try { & (Join-Path $relocated 'Stop_Demo.ps1') } catch { Write-Warning $_.Exception.Message }
    }
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\TangdaoLauncherValidation_'
    if ($resolvedTestRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedTestRoot)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
