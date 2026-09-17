param(
    [string]$ApplicationPath,
    [string]$InnoCompiler,
    [string]$Python = 'python',
    [string]$OutputDirectory,
    [string]$Version = '1.3.0'
)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
if (-not $ApplicationPath) { $ApplicationPath = Join-Path $repository 'dist\SimPy Lab Studio.exe' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repository 'dist' }
$ApplicationPath = (Resolve-Path -LiteralPath $ApplicationPath).Path
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must have three numeric components.' }
if ([Diagnostics.FileVersionInfo]::GetVersionInfo($ApplicationPath).ProductName -ne 'SimPy Lab Studio') {
    throw 'ApplicationPath must point to the built SimPy Lab Studio executable.'
}
if ([Diagnostics.FileVersionInfo]::GetVersionInfo($ApplicationPath).ProductVersion -ne $Version) {
    throw 'Installer version must match the desktop executable product version.'
}

if (-not $InnoCompiler) {
    $candidates = @(
        $env:ISCC_PATH,
        (Join-Path $repository 'build\installer-deps\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $InnoCompiler = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $InnoCompiler) { throw 'Install Inno Setup 6.7+ or pass -InnoCompiler with the path to ISCC.exe.' }
$InnoCompiler = (Resolve-Path -LiteralPath $InnoCompiler).Path
$dependencies = Join-Path $repository 'build\installer-deps'
New-Item -ItemType Directory -Path $dependencies,$OutputDirectory -Force | Out-Null
$bootstrapper = Join-Path $dependencies 'MicrosoftEdgeWebview2Setup.exe'
if (-not (Test-Path -LiteralPath $bootstrapper)) {
    $download = $bootstrapper + '.download'
    Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $download
    Move-Item -LiteralPath $download -Destination $bootstrapper
}
$signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
    throw 'WebView2 bootstrapper must have a valid Microsoft Authenticode signature.'
}
& $Python (Join-Path $PSScriptRoot 'make_installer_images.py')
if ($LASTEXITCODE -ne 0) { throw 'Installer artwork generation failed.' }
& $InnoCompiler "/DAppExe=$ApplicationPath" "/DWebView2Bootstrapper=$bootstrapper" "/DSetupOutput=$OutputDirectory" "/DAppVersion=$Version" (Join-Path $repository 'installer\studio.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
$installer = Join-Path $OutputDirectory "SimPy-Lab-Studio-Setup-$Version.exe"
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($installer + '.sha256'), "$hash  $([IO.Path]::GetFileName($installer))`r`n", [Text.Encoding]::ASCII)
$manifest = [ordered]@{
    version = $Version
    installer_sha256 = $hash
    application_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ApplicationPath).Hash.ToLowerInvariant()
    webview2_bootstrapper_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $bootstrapper).Hash.ToLowerInvariant()
    webview2_bootstrapper_signer = $signature.SignerCertificate.Subject
    compiler_version = [Diagnostics.FileVersionInfo]::GetVersionInfo($InnoCompiler).FileVersion
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'installer-build.json') -Encoding UTF8
Write-Output "Installer created: $installer"
