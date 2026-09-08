param(
    [string]$SigningThumbprint = $env:PERSONAL_HUB_SIGNING_THUMBPRINT,
    [string]$ManifestSigningKey = $env:PERSONAL_HUB_MANIFEST_SIGNING_KEY,
    [string]$ManifestKeyId = $env:PERSONAL_HUB_MANIFEST_KEY_ID,
    [string]$UpdateBaseUrl = $env:PERSONAL_HUB_UPDATE_BASE_URL,
    [switch]$RequireClean
)

$ErrorActionPreference = "Stop"
$hubRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $hubRoot

python -m pip install -e ".[desktop,build,test,dev,ui-test]"
if ($LASTEXITCODE -ne 0) { throw "Personal Hub build dependencies failed" }

python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { throw "Personal Hub format gate failed" }
python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "Personal Hub lint gate failed" }
python scripts/check_documentation_consistency.py
if ($LASTEXITCODE -ne 0) { throw "Documentation gate failed" }
python scripts/check_personal_hub_imports.py --output .benchmark-data/personal-hub-imports.json
if ($LASTEXITCODE -ne 0) { throw "Personal Hub import boundary failed" }

$env:QT_QPA_PLATFORM = "offscreen"
powershell -ExecutionPolicy Bypass -File scripts/test_personal_hub.ps1 -Lane all
if ($LASTEXITCODE -ne 0) { throw "Personal Hub regression gate failed" }
python scripts/run_personal_hub_reliability.py --mode accelerated --output .benchmark-data/personal-hub-seven-day.json
if ($LASTEXITCODE -ne 0) { throw "Personal Hub bounded-growth reliability gate failed" }

python -m PyInstaller --clean --noconfirm packaging/personal-hub.spec
if ($LASTEXITCODE -ne 0) { throw "Personal Hub PyInstaller build failed" }
python scripts/verify_personal_hub_package.py dist/PersonalHub.exe --analysis build/personal-hub/Analysis-00.toc --output .benchmark-data/personal-hub-package.json
if ($LASTEXITCODE -ne 0) { throw "Personal Hub packaged dependency boundary failed" }

$hubAuthenticodeSigned = $false
if ($SigningThumbprint) {
    $hubSignTool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $hubSignTool) { throw "signtool.exe is required when a signing thumbprint is supplied" }
    & $hubSignTool.Source sign /sha1 $SigningThumbprint /fd SHA256 /tr https://timestamp.digicert.com /td SHA256 dist\PersonalHub.exe
    if ($LASTEXITCODE -ne 0) { throw "Personal Hub Authenticode signing failed" }
    & $hubSignTool.Source verify /pa /all dist\PersonalHub.exe
    if ($LASTEXITCODE -ne 0) { throw "Personal Hub Authenticode verification failed" }
    $hubAuthenticodeSigned = $true
}

$hubIscc = Get-Command iscc -ErrorAction SilentlyContinue
$hubIsccPath = if ($hubIscc) { $hubIscc.Source } else { $null }
if (-not $hubIsccPath) {
    $hubInnoCandidate = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (Test-Path -LiteralPath $hubInnoCandidate) { $hubIsccPath = $hubInnoCandidate }
}
$hubInstaller = "dist\personal-hub\PersonalHub-0.1.0-Setup.exe"
if ($hubIsccPath) {
    & $hubIsccPath packaging\personal-hub-installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Personal Hub installer build failed" }
    if ($hubAuthenticodeSigned) {
        & $hubSignTool.Source sign /sha1 $SigningThumbprint /fd SHA256 /tr https://timestamp.digicert.com /td SHA256 $hubInstaller
        if ($LASTEXITCODE -ne 0) { throw "Personal Hub installer signing failed" }
        & $hubSignTool.Source verify /pa /all $hubInstaller
        if ($LASTEXITCODE -ne 0) { throw "Personal Hub installer signature verification failed" }
    }
} else {
    Write-Warning "Inno Setup was not found. The separately defined installer was not compiled."
}

$hubReleaseArguments = @(
    "scripts/generate_personal_hub_release.py",
    "--artifact", "dist/PersonalHub.exe",
    "--output-dir", "dist/personal-hub/release"
)
if (Test-Path -LiteralPath $hubInstaller) {
    $hubReleaseArguments += @("--artifact", $hubInstaller)
}
if ($hubAuthenticodeSigned) { $hubReleaseArguments += "--authenticode-signed" }
if ($RequireClean) { $hubReleaseArguments += "--require-clean" }
if ($ManifestSigningKey -or $ManifestKeyId -or $UpdateBaseUrl) {
    if (-not ($ManifestSigningKey -and $ManifestKeyId -and $UpdateBaseUrl)) {
        throw "Manifest signing key, key ID, and HTTPS update base URL must be supplied together"
    }
    $hubReleaseArguments += @(
        "--signing-key", $ManifestSigningKey,
        "--key-id", $ManifestKeyId,
        "--artifact-url-base", $UpdateBaseUrl
    )
}
python @hubReleaseArguments
if ($LASTEXITCODE -ne 0) { throw "Personal Hub release evidence generation failed" }

git diff --check
if ($LASTEXITCODE -ne 0) { throw "Git whitespace gate failed" }
if (-not $hubAuthenticodeSigned) {
    Write-Warning "Unsigned Personal Hub candidate only. Do not publish until Authenticode and detached manifest signatures are verified."
}
Write-Host "Personal Hub candidate artifacts are under dist/personal-hub/."
