param([Parameter(Mandatory=$true)][string]$InstallerPath)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$qaDirectory = Join-Path $repository 'outputs\installer-qa'
$installDirectory = [IO.Path]::GetFullPath((Join-Path $qaDirectory 'installed-app'))
if (-not $installDirectory.StartsWith(([IO.Path]::GetFullPath($qaDirectory) + '\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Test installation must stay in the workspace QA directory.'
}
$registryKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{69F4E6BE-D651-4A60-97F4-8D34D96A7D10}_is1'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'SimPy Lab Studio.lnk'
$menuDirectory = Join-Path ([Environment]::GetFolderPath('Programs')) 'SimPy Lab Studio'
if ((Test-Path $registryKey) -or (Test-Path -LiteralPath $desktopShortcut) -or (Test-Path -LiteralPath $menuDirectory)) {
    throw 'An installation or shortcut already exists. Refusing to overwrite it during QA.'
}
if (Get-Process -Name 'SimPy Lab Studio' -ErrorAction SilentlyContinue) {
    throw 'Close Studio before testing installation so its AppMutex does not block setup.'
}
New-Item -ItemType Directory -Path $qaDirectory -Force | Out-Null
$userData = Join-Path $env:LOCALAPPDATA 'SimPy Lab Studio'
function Read-UserDataHashes {
    $hashes = @{}
    if (Test-Path -LiteralPath $userData) {
        Get-ChildItem -LiteralPath $userData -Filter *.json -Recurse -File | ForEach-Object {
            $hashes[$_.FullName] = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
        }
    }
    return $hashes
}
$before = Read-UserDataHashes
$uninstaller = Join-Path $installDirectory 'unins000.exe'
$installedExe = Join-Path $installDirectory 'SimPy Lab Studio.exe'
$report = [ordered]@{ok=$false;checks=@()}
try {
    foreach ($phase in @('install','reinstall')) {
        $arguments = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LANG=chinesesimp',
            '/TASKS=desktopicon',
            ('/DIR="' + $installDirectory + '"'),
            ('/LOG="' + (Join-Path $qaDirectory "$phase.log") + '"'))
        $process = Start-Process -FilePath $InstallerPath -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "$phase failed: exit $($process.ExitCode)" }
        if (-not (Test-Path -LiteralPath $installedExe)) { throw 'Installed executable missing.' }
        if (-not (Test-Path -LiteralPath $uninstaller)) { throw 'Uninstaller missing.' }
        if (-not (Test-Path -LiteralPath $desktopShortcut)) { throw 'Desktop shortcut missing.' }
        if (-not (Test-Path -LiteralPath (Join-Path $menuDirectory 'SimPy Lab Studio.lnk'))) { throw 'Start menu shortcut missing.' }
        $record = Get-ItemProperty $registryKey
        if ($record.InstallLocation.TrimEnd('\') -ne $installDirectory) { throw 'Uninstall record points to the wrong directory.' }
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($desktopShortcut)
        if ($shortcut.TargetPath -ne $installedExe) { throw 'Shortcut points to the wrong executable.' }
        $report.checks += $phase
    }
    $smokeReport = Join-Path $qaDirectory 'installed-self-test.json'
    $smokeData = Join-Path $qaDirectory 'installed-self-test-data'
    $process = Start-Process -FilePath $installedExe -ArgumentList @(
        '--self-test', ('"' + $smokeReport + '"'), '--data-dir', ('"' + $smokeData + '"')
    ) -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'Installed application self-test failed.' }
    $smoke = Get-Content -LiteralPath $smokeReport -Raw | ConvertFrom-Json
    if (-not $smoke.ok -or -not $smoke.frozen -or -not $smoke.server_stopped) { throw 'Installed self-test returned invalid results.' }
    $report.checks += @('native shortcuts','uninstall registration','installed EXE real simulation')
    $report.application_sha256 = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash.ToLowerInvariant()
}
finally {
    if (Test-Path -LiteralPath $uninstaller) {
        $process = Start-Process -FilePath $uninstaller -ArgumentList @(
            '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
            ('/LOG="' + (Join-Path $qaDirectory 'uninstall.log') + '"')
        ) -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw 'Test uninstallation failed.' }
    }
}
if ((Test-Path -LiteralPath $installedExe) -or (Test-Path $registryKey) -or (Test-Path -LiteralPath $desktopShortcut)) {
    throw 'Uninstall left installed files, registration or desktop shortcut.'
}
if (Test-Path -LiteralPath (Join-Path $menuDirectory 'SimPy Lab Studio.lnk')) { throw 'Uninstall left the start menu shortcut.' }
$after = Read-UserDataHashes
if ($before.Count -ne $after.Count) { throw 'Installer changed the user data file list.' }
foreach($path in $before.Keys) { if ($before[$path] -ne $after[$path]) { throw 'Installer changed user data.' } }
$report.ok = $true
$report.checks += @('uninstall','user data preserved')
$report.preserved_user_json_files = $before.Count
$report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $qaDirectory 'installer-check.json') -Encoding UTF8
$report | ConvertTo-Json -Depth 4
