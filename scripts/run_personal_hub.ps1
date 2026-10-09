param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = "Stop"
$hubRoot = Split-Path -Parent $PSScriptRoot
$previousPythonPath = $env:PYTHONPATH
$sourcePath = Join-Path $hubRoot "src"
$env:PYTHONPATH = if ($previousPythonPath) { "$sourcePath;$previousPythonPath" } else { $sourcePath }

try {
    & python -m personal_hub.app @AppArguments
    $appExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
exit $appExitCode
