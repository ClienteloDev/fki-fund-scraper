param(
    [switch]$SkipPreflight,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

$UvArguments = @(
    "run",
    "python",
    "scripts/run_mvp_final.py"
)

if ($SkipPreflight) {
    $UvArguments += "--skip-preflight"
}

if ($Force) {
    $UvArguments += "--force"
}

& uv @UvArguments
exit $LASTEXITCODE
