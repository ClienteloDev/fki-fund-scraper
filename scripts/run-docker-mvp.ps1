param(
    [int]$Concurrency = 6,
    [int]$DocumentConcurrency = 4,
    [switch]$SkipBuild,
    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path `
    -Parent `
    (Split-Path -Parent $PSScriptRoot)

Set-Location $ProjectRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw (
        "Docker was not found. Install Docker Desktop " +
        "and open a new PowerShell window."
    )
}

docker compose version | Out-Null

if (-not $SkipBuild) {
    Write-Host ""
    Write-Host "Building Docker image..."
    Write-Host ""

    docker compose build

    if ($LASTEXITCODE -ne 0) {
        throw "Docker image build failed."
    }
}

if (-not $SkipPreflight) {
    Write-Host ""
    Write-Host "Validating input..."
    Write-Host ""

    docker compose run `
        --rm `
        fundscraper `
        validate-input `
        data/input/funds.json

    if ($LASTEXITCODE -ne 0) {
        throw "Input validation failed."
    }
}

Write-Host ""
Write-Host "Running optimized scraper..."
Write-Host ""

docker compose run `
    --rm `
    --entrypoint python `
    fundscraper `
    scripts/run_mvp_final.py `
    --concurrency $Concurrency `
    --document-concurrency $DocumentConcurrency `
    $(if ($SkipPreflight) { "--skip-preflight" })

if ($LASTEXITCODE -ne 0) {
    throw "Scraper execution failed."
}

Write-Host ""
Write-Host "Validating final output..."
Write-Host ""

docker compose run `
    --rm `
    fundscraper `
    validate-output `
    data/output/funds.enriched.json

if ($LASTEXITCODE -ne 0) {
    throw "Final output validation failed."
}

Write-Host ""
Write-Host "Docker run completed successfully."
Write-Host "Output: data/output/funds.enriched.json"
Write-Host ""