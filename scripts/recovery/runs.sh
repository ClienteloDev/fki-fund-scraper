#!/usr/bin/env bash
# Batch-06 scraper diagnostic pass: one isolated run-fund per fund.
# Nothing is written to cache/regen.sqlite3, cache/http or cache/parsed.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 1

LOG="$ROOT/cache/recovery-online/runs"
mkdir -p "$LOG"

run() {
  ws="cache/recovery-online/$1"
  name="$2"
  mkdir -p "$ws/output"
  start=$(date +%s)
  uv run fundscraper run-fund "$name" \
    --database "$ws/db.sqlite3" \
    --output "$ws/output/enriched.json" \
    --cache-directory "$ws/http" \
    --parsed-directory "$ws/parsed" \
    --max-pages 40 --max-depth 3 --max-documents 40 \
    > "$LOG/$1.log" 2>&1
  rc=$?
  end=$(date +%s)
  echo "$1 rc=$rc seconds=$((end-start))" >> "$LOG/summary.txt"
}

run b06-01-convenio "Convenio, investiční fond s proměnným základním kapitálem, a.s."
run b06-02-good-value-investments "Good Value Investments SICAV, a.s."
run b06-03-creditas-assets "CREDITAS ASSETS SICAV a.s."
run b06-04-evermore-capital-management "Evermore Capital Management a.s., SICAV"
run b06-05-falanga-invest "Falanga Invest SICAV a.s."
run b06-06-energy-financial-group-fund "Energy financial group Fund SICAV a.s."
run b06-07-adversum "Adversum SICAV, a.s."
run b06-08-bhs-iconic-cars "BHS ICONIC CARS SICAV, a.s."
run b06-09-bidli "BIDLI investiční fond SICAV, a.s."
run b06-10-reticulum "Reticulum Fund SICAV, a.s."
echo "DONE" >> "$LOG/summary.txt"
