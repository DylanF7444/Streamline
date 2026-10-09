param(
    [ValidateSet("fast", "core", "ui", "integration", "architecture", "release", "phase1", "phase2", "phase3", "phase4", "phase5", "phase6", "phase7", "all")]
    [string]$Lane = "fast",
    [switch]$Coverage
)

$ErrorActionPreference = "Stop"
$hubRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $hubRoot
$sourcePath = Join-Path $hubRoot "src"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$sourcePath;$env:PYTHONPATH" } else { $sourcePath }

if ($Lane -in @("ui", "release", "phase2", "phase3", "phase4", "phase5", "phase6", "phase7", "all")) {
    $env:QT_QPA_PLATFORM = "offscreen"
}

$pytestArguments = @()
switch ($Lane) {
    "fast" {
        $pytestArguments = @("tests", "--ignore=tests/ui", "-m", "hub_fast", "-q")
    }
    "core" {
        $pytestArguments = @("tests", "--ignore=tests/ui", "-m", "hub_core", "-q")
    }
    "ui" {
        $pytestArguments = @("tests/ui", "-m", "hub_ui", "-q")
    }
    "integration" {
        $pytestArguments = @("tests", "-m", "hub_integration", "-q")
    }
    "architecture" {
        $pytestArguments = @("tests", "-m", "hub_architecture", "-q")
    }
    "release" {
        $pytestArguments = @("tests", "-m", "hub_release", "-q")
    }
    "all" {
        $pytestArguments = @("tests", "-q")
    }
    default {
        $pytestArguments = @("tests", "-m", "hub_$Lane", "-q")
    }
}

if ($Coverage) {
    $pytestArguments += @("--cov=personal_hub", "--cov-branch", "--cov-report=term-missing")
}

$pytestArguments = @("--strict-markers") + $pytestArguments
& python -m pytest @pytestArguments
exit $LASTEXITCODE
