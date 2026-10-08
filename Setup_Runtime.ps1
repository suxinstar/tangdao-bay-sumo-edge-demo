param([switch]$PortableOnly)
$ErrorActionPreference = 'Stop'
$demoRoot = $PSScriptRoot
$runtimeRoot = Join-Path $demoRoot '_runtime'
$manifest = Get-Content -LiteralPath (Join-Path $demoRoot 'runtime-manifest.json') -Raw | ConvertFrom-Json
if (![Environment]::Is64BitOperatingSystem -or $env:OS -ne 'Windows_NT') {
    throw 'This launcher supports Windows x64. See README for manual setup on other systems.'
}

function Test-DemoPython([string]$Path) {
    if (!$Path -or !(Test-Path -LiteralPath $Path -PathType Leaf) -or $Path -match '\\WindowsApps\\') { return $false }
    try {
        & $Path -c 'import sys, json, ssl, http.server; raise SystemExit(0 if sys.version_info >= (3,10) and sys.maxsize > 2**32 else 1)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Test-DemoSumo([string]$Path) {
    if (!$Path -or !(Test-Path -LiteralPath (Join-Path $Path 'bin\sumo.exe')) -or
        !(Test-Path -LiteralPath (Join-Path $Path 'tools\traci\__init__.py'))) { return $false }
    try {
        $versionText = (& (Join-Path $Path 'bin\sumo.exe') --version 2>$null | Out-String)
        return $LASTEXITCODE -eq 0 -and $versionText -match '(?:Version\s+|Eclipse SUMO sumo\s+)(\d+\.\d+\.\d+)' -and
            [version]$Matches[1] -ge [version]$manifest.sumoMinimum
    } catch { return $false }
}

function Test-DemoNumpy([string]$Path) {
    try {
        & $Path -c 'import numpy; assert numpy.__version__' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Get-VerifiedArchive($Dependency) {
    $uri = [uri]$Dependency.url
    if ($uri.Scheme -ne 'https' -or $uri.Host -notin @('www.python.org', 'sumo.dlr.de', 'files.pythonhosted.org')) {
        throw 'Runtime download URL must be an approved official HTTPS URL.'
    }
    $cache = Join-Path $runtimeRoot 'downloads'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $archive = Join-Path $cache $Dependency.archive
    if (!(Test-Path -LiteralPath $archive)) {
        $partial = "$archive.partial.$PID"
        Write-Host ("Downloading {0} ({1:N1} MiB) from {2} ..." -f $Dependency.archive, ($Dependency.bytes / 1MB), $uri.Host)
        try {
            $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
            if ($curl) {
                & $curl.Source --fail --location --retry 2 --connect-timeout 30 --max-time 1800 --output $partial $Dependency.url
                if ($LASTEXITCODE -ne 0) { throw "Download exited with code $LASTEXITCODE" }
            } else {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $savedProgress = $ProgressPreference
                try { $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri $Dependency.url -OutFile $partial -TimeoutSec 1800 }
                finally { $ProgressPreference = $savedProgress }
            }
        } catch {
            throw "Cannot download $($Dependency.archive). Check the internet connection/proxy and retry Start_Demo.cmd, or use the Windows offline package. Details: $($_.Exception.Message)"
        }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Dependency.sha256) {
            throw "SHA256 mismatch for $($Dependency.archive); the file was NOT extracted. Retry after removing the .partial file, or use the official offline release."
        }
        Move-Item -LiteralPath $partial -Destination $archive
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Dependency.sha256) {
        throw "Cached archive failed SHA256: $archive. Remove only this archive and retry; nothing was extracted."
    }
    Write-Host "Verified SHA256: $($Dependency.archive)"
    return $archive
}

function Install-PortableDependency($Dependency, [switch]$IsSumo) {
    $archive = Get-VerifiedArchive $Dependency
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $staging = Join-Path $runtimeRoot ('.staging-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    $stagingPrefix = [IO.Path]::GetFullPath($staging).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    Write-Host "Preparing $($Dependency.directory) ..."
    try {
        foreach ($entry in $zip.Entries) {
            $relative = $entry.FullName.Replace('\', '/')
            if ($IsSumo) {
                if (!$relative.StartsWith($Dependency.archiveRoot, [StringComparison]::Ordinal)) { throw 'Unexpected SUMO archive layout.' }
                $relative = $relative.Substring($Dependency.archiveRoot.Length)
                if (!$relative) { continue }
                $topLevel = ($relative -split '/')[0]
                $included = $topLevel -in $Dependency.includedDirectories -or $relative -in $Dependency.additionalFiles -or
                    ($Dependency.includeRootFiles -and !$relative.Contains('/'))
                if (!$included) { continue }
            }
            if (!$relative) { continue }
            $destination = [IO.Path]::GetFullPath((Join-Path $staging $relative))
            if (!$destination.StartsWith($stagingPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe archive path rejected.' }
            if ($entry.FullName.EndsWith('/')) {
                New-Item -ItemType Directory -Path $destination -Force | Out-Null
            } else {
                New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination)
            }
        }
    } finally { $zip.Dispose() }
    $target = [IO.Path]::GetFullPath((Join-Path $runtimeRoot $Dependency.directory))
    $allowedRoot = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\') + '\'
    if (!$target.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime target is outside _runtime.' }
    if (Test-Path -LiteralPath $target) {
        # Preserve incomplete installations for diagnosis instead of deleting them.
        Move-Item -LiteralPath $target -Destination ($target + '.incomplete-' + [guid]::NewGuid().ToString('N'))
    }
    Move-Item -LiteralPath $staging -Destination $target
    [pscustomobject]@{version=$Dependency.version;archive=$Dependency.archive;sha256=$Dependency.sha256} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $target 'tangdao-runtime.json') -Encoding UTF8
    return $target
}

$portablePythonRoot = Join-Path $runtimeRoot $manifest.python.directory
$portablePython = Join-Path $portablePythonRoot 'python.exe'
$portableSumo = Join-Path $runtimeRoot $manifest.sumo.directory
$pythonExe = $null
$sumoRoot = $null

if (!$PortableOnly) {
    $pythonCandidates = @($env:TANGDAO_PYTHON, (Join-Path $demoRoot '.venv\Scripts\python.exe'), $portablePython) | Where-Object { $_ }
    $pythonCandidates += @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($candidate in $pythonCandidates | Select-Object -Unique) {
        if (Test-DemoPython $candidate) { $pythonExe = [IO.Path]::GetFullPath($candidate); break }
    }
    $sumoCandidates = @($env:SUMO_HOME, $portableSumo, 'C:\Program Files (x86)\Eclipse\Sumo', 'C:\Program Files\Eclipse\Sumo') | Where-Object { $_ }
    $sumoCommand = Get-Command sumo.exe -ErrorAction SilentlyContinue
    if ($sumoCommand) { $sumoCandidates += Split-Path -Parent (Split-Path -Parent $sumoCommand.Source) }
    foreach ($candidate in $sumoCandidates | Select-Object -Unique) {
        if (Test-DemoSumo $candidate) { $sumoRoot = [IO.Path]::GetFullPath($candidate); break }
    }
}

if (!$pythonExe) {
    if (!(Test-DemoPython $portablePython)) { $portablePythonRoot = Install-PortableDependency $manifest.python }
    # Embedded Python ignores PYTHONPATH. These relative entries survive moving the folder.
    @('python312.zip', '.', '..\..') | Set-Content -LiteralPath (Join-Path $portablePythonRoot 'python312._pth') -Encoding ASCII
    $pythonExe = Join-Path $portablePythonRoot 'python.exe'
    if (!(Test-DemoPython $pythonExe)) { throw 'Portable Python did not start. Extract the project to a writable local folder and retry.' }
} elseif ($pythonExe -eq $portablePython) {
    @('python312.zip', '.', '..\..') | Set-Content -LiteralPath (Join-Path $portablePythonRoot 'python312._pth') -Encoding ASCII
}
if (!$sumoRoot) {
    if (!(Test-DemoSumo $portableSumo)) { $portableSumo = Install-PortableDependency $manifest.sumo -IsSumo }
    $sumoRoot = $portableSumo
    if (!(Test-DemoSumo $sumoRoot)) { throw 'Portable SUMO did not start. See README troubleshooting and check antivirus quarantine.' }
}
# The learned scheduler needs NumPy. Preserve external Python installations;
# if NumPy is missing, prepare our portable interpreter instead of global pip.
if (!(Test-DemoNumpy $pythonExe) -or $pythonExe -eq $portablePython) {
    if (!(Test-DemoPython $portablePython)) { $portablePythonRoot = Install-PortableDependency $manifest.python }
    $numpyRoot = Join-Path $runtimeRoot $manifest.numpy.directory
    if (!(Test-Path -LiteralPath (Join-Path $numpyRoot 'numpy\__init__.py'))) {
        $numpyRoot = Install-PortableDependency $manifest.numpy
    }
    @('python312.zip', '.', '..\..', ('..\' + $manifest.numpy.directory)) |
        Set-Content -LiteralPath (Join-Path $portablePythonRoot 'python312._pth') -Encoding ASCII
    $pythonExe = $portablePython
    if (!(Test-DemoNumpy $pythonExe)) { throw 'Portable NumPy did not start. See _runtime and retry Setup_Runtime.ps1.' }
}
$pythonVersion = (& $pythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" | Out-String).Trim()
Write-Host "Python $pythonVersion : $pythonExe"
Write-Host "SUMO: $sumoRoot"
return [pscustomobject]@{
    python=$pythonExe;pythonVersion=$pythonVersion;sumo=$sumoRoot
    portablePython=($pythonExe -eq $portablePython);portableSumo=($sumoRoot -eq (Join-Path $runtimeRoot $manifest.sumo.directory))
}
