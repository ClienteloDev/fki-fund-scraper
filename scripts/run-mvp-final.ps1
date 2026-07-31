param(
    [ValidateRange(1, 20)]
    [int]$Concurrency = 6,

    [ValidateRange(1, 20)]
    [int]$DocumentConcurrency = 4,

    [switch]$SkipPreflight,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (
    Resolve-Path (
        Join-Path $PSScriptRoot ".."
    )
).Path

Set-Location $ProjectRoot

$UvArguments = @(
    "run",
    "python",
    "scripts/run_mvp_final.py",
    "--concurrency",
    $Concurrency.ToString(),
    "--document-concurrency",
    $DocumentConcurrency.ToString()
)

if ($SkipPreflight) {
    $UvArguments += "--skip-preflight"
}

if ($Force) {
    $UvArguments += "--force"
}

& uv @UvArguments
exit $LASTEXITCODE
