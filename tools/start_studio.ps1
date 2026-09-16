param([switch]$NoBrowser, [int]$Port = 8765)

$ErrorActionPreference = 'Stop'
$studioRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $studioRoot
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
$studioUrl = "http://127.0.0.1:$Port"
try {
    $existingStudio = Invoke-RestMethod -Uri "$studioUrl/api/studio/bootstrap" -TimeoutSec 2
} catch {
    $existingStudio = $null
}
if ($existingStudio -and $existingStudio.template -and $existingStudio.ai) {
    Write-Host "SimLab Studio is already running at $studioUrl"
    if (-not $NoBrowser) { Start-Process $studioUrl }
    exit 0
}
$studioPython = Join-Path $studioRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $studioPython)) {
    Write-Host 'Creating the local Python environment...'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        $installedPython = $null
        if ($pythonCommand -and $pythonCommand.Source -notlike '*\WindowsApps\*') {
            $installedPython = $pythonCommand.Source
        } else {
            $pythonFolder = Join-Path $env:LOCALAPPDATA 'Programs\Python'
            if (Test-Path -LiteralPath $pythonFolder) {
                $installedPython = Get-ChildItem -LiteralPath $pythonFolder -Directory |
                    Where-Object { $_.Name -match '^Python3\d+$' } |
                    Sort-Object { [int]($_.Name -replace '^Python', '') } -Descending |
                    ForEach-Object { Join-Path $_.FullName 'python.exe' } |
                    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
            }
        }
        if (-not $installedPython) {
            throw 'Install Python 3.11 or newer, then start this application again.'
        }
        & $installedPython -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
# Always prefer this checkout over any other editable installation.
$env:PYTHONPATH = Join-Path $studioRoot 'src'
$env:PYTHONIOENCODING = 'utf-8'
& $studioPython -c "import sys; assert sys.version_info >= (3,11); import fastapi, uvicorn, simlab.studio"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing SimLab Studio dependencies (first launch)...'
    & $studioPython -m pip install -e '.[studio]'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$studioArguments = @('-m', 'simlab.cli', 'studio', '--port', "$Port")
if (-not $NoBrowser) { $studioArguments += '--open-browser' }
& $studioPython @studioArguments
if ($LASTEXITCODE -ne 0) { throw 'Studio could not start. See the error above.' }
