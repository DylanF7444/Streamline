param(
    [Parameter(Mandatory = $true)]
    [string]$Installer,
    [string]$AssistantExecutable,
    [string]$AssistantDatabase,
    [string]$Output = ".benchmark-data/personal-hub-installer-lifecycle.json"
)

$ErrorActionPreference = "Stop"
$hubInstallerPath = (Resolve-Path -LiteralPath $Installer).Path
$hubVerificationRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("PersonalHubInstallerVerification-" + [guid]::NewGuid().ToString("N"))
$hubVerificationRoot = [System.IO.Path]::GetFullPath($hubVerificationRoot)
$hubTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
if (-not $hubVerificationRoot.StartsWith($hubTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer verification root is outside the system temporary directory"
}
$hubInstallDir = Join-Path $hubVerificationRoot "install"
$hubDataDir = Join-Path $hubVerificationRoot "PersonalHub"
New-Item -ItemType Directory -Path $hubDataDir -Force | Out-Null

function Get-HubOptionalHash([string]$PathValue) {
    if (-not $PathValue) { return $null }
    $hubResolved = (Resolve-Path -LiteralPath $PathValue).Path
    return (Get-FileHash -LiteralPath $hubResolved -Algorithm SHA256).Hash
}

function Invoke-HubInstaller([string[]]$Arguments) {
    $hubProcess = Start-Process -FilePath $hubInstallerPath -ArgumentList $Arguments -PassThru -Wait -WindowStyle Hidden
    if ($hubProcess.ExitCode -ne 0) { throw "Personal Hub installer returned $($hubProcess.ExitCode)" }
}

function Invoke-HubUninstaller([string]$DeleteChoice) {
    $hubUninstaller = Join-Path $hubInstallDir "unins000.exe"
    if (-not (Test-Path -LiteralPath $hubUninstaller)) { throw "Personal Hub uninstaller is missing" }
    $hubArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DELETEUSERDATA=$DeleteChoice",
        "/HUBDATADIR=`"$hubDataDir`""
    )
    $hubProcess = Start-Process -FilePath $hubUninstaller -ArgumentList $hubArguments -PassThru -Wait -WindowStyle Hidden
    if ($hubProcess.ExitCode -ne 0) { throw "Personal Hub uninstaller returned $($hubProcess.ExitCode)" }
}

$hubAssistantExeBefore = Get-HubOptionalHash $AssistantExecutable
$hubAssistantDbBefore = Get-HubOptionalHash $AssistantDatabase
$hubAssistantStartupBefore = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "LocalAssistant" -ErrorAction SilentlyContinue).LocalAssistant
$hubPassed = $false
try {
    $hubInstallArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=`"$hubInstallDir`"",
        "/HUBDATADIR=`"$hubDataDir`""
    )
    Invoke-HubInstaller $hubInstallArguments
    $hubExecutable = Join-Path $hubInstallDir "PersonalHub.exe"
    if (-not (Test-Path -LiteralPath $hubExecutable)) { throw "Clean install did not create PersonalHub.exe" }
    $hubFirstHash = (Get-FileHash -LiteralPath $hubExecutable -Algorithm SHA256).Hash

    Invoke-HubInstaller $hubInstallArguments
    $hubUpgradeHash = (Get-FileHash -LiteralPath $hubExecutable -Algorithm SHA256).Hash
    if ($hubFirstHash -ne $hubUpgradeHash) { throw "Idempotent same-version upgrade changed executable bytes" }

    $hubPreservedMarker = Join-Path $hubDataDir "preserve.marker"
    Set-Content -LiteralPath $hubPreservedMarker -Value "preserve" -Encoding utf8
    Invoke-HubUninstaller "no"
    if (Test-Path -LiteralPath $hubExecutable) { throw "Uninstall did not remove PersonalHub.exe" }
    if (-not (Test-Path -LiteralPath $hubPreservedMarker)) { throw "Preserve-data uninstall removed Hub data" }

    Invoke-HubInstaller $hubInstallArguments
    Invoke-HubUninstaller "yes"
    if (Test-Path -LiteralPath $hubDataDir) { throw "Delete-data uninstall retained Hub data" }

    if ((Get-HubOptionalHash $AssistantExecutable) -ne $hubAssistantExeBefore) { throw "Local Assistant executable changed" }
    if ((Get-HubOptionalHash $AssistantDatabase) -ne $hubAssistantDbBefore) { throw "Local Assistant database changed" }
    $hubAssistantStartupAfter = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "LocalAssistant" -ErrorAction SilentlyContinue).LocalAssistant
    if ($hubAssistantStartupAfter -ne $hubAssistantStartupBefore) { throw "Local Assistant startup entry changed" }
    $hubPersonalStartup = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PersonalHub" -ErrorAction SilentlyContinue).PersonalHub
    if ($hubPersonalStartup) { throw "Personal Hub startup entry remained after uninstall" }
    $hubPassed = $true
} finally {
    if (Test-Path -LiteralPath $hubVerificationRoot) {
        $hubResolvedCleanup = [System.IO.Path]::GetFullPath($hubVerificationRoot)
        if (-not $hubResolvedCleanup.StartsWith($hubTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean an installer verification path outside the temporary root"
        }
        Remove-Item -LiteralPath $hubResolvedCleanup -Recurse -Force
    }
}

$hubReport = [ordered]@{
    schema_version = 1
    verified_at = [DateTime]::UtcNow.ToString("o")
    installer = [System.IO.Path]::GetFileName($hubInstallerPath)
    clean_install = "passed"
    same_version_upgrade = "passed"
    preserve_data_uninstall = "passed"
    delete_data_uninstall = "passed"
    startup_registration_cleanup = "passed"
    local_assistant_unchanged = "passed"
    passes = $hubPassed
}
$hubOutputPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))
$hubOutputDirectory = Split-Path -Parent $hubOutputPath
New-Item -ItemType Directory -Path $hubOutputDirectory -Force | Out-Null
$hubReport | ConvertTo-Json | Set-Content -LiteralPath $hubOutputPath -Encoding utf8
Write-Host ($hubReport | ConvertTo-Json)
