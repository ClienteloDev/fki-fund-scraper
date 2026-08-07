# Fundscraper

Aplikace získává veřejně dostupné informace o českých fondech kvalifikovaných investorů.

Pro každý fond se pokouší dohledat:

- investiční horizont,
- minimální investici,
- cílový výnos,
- poplatky,
- objem majetku pod správou.

Scraper nejdříve prochází oficiální web fondu a jeho dokumenty. Následně může použít adaptéry pro konkrétní správce a administrátory fondů.

## Požadavky

Doporučený způsob spuštění používá Docker.

Je potřeba mít nainstalované:

- Docker Desktop na Windows,
- Docker Compose,
- připojení k internetu.

Pro Docker spuštění není potřeba lokálně instalovat Python ani projektové knihovny.

## Vstup

Vstupní soubor:

```text
data/input/funds.json
```

Každá položka obsahuje název fondu a jeho webovou stránku:

```json
[
  {
    "name": "Název fondu",
    "web": "https://example.cz"
  }
]
```

## Rychlé spuštění pomocí Dockeru

Sestavení image:

```powershell
docker compose build
```

Kontrola CLI:

```powershell
docker compose run --rm fundscraper --help
```

Validace vstupu:

```powershell
docker compose run --rm fundscraper `
    validate-input `
    data/input/funds.json
```

Kompletní běh lze spustit připraveným PowerShell skriptem:

```powershell
powershell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File scripts/run-docker-mvp.ps1
```

S nastavením paralelního zpracování:

```powershell
powershell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File scripts/run-docker-mvp.ps1 `
    -Concurrency 6 `
    -DocumentConcurrency 4
```

## Výstup

Hlavní výsledný soubor:

```text
data/output/funds.enriched.json
```

Reporty:

```text
reports/
```

Databáze a cache:

```text
cache/
```

Tyto adresáře jsou připojené jako Docker volumes, takže jejich obsah zůstane dostupný i po ukončení kontejneru.

## Validace výstupu

```powershell
docker compose run --rm fundscraper `
    validate-output `
    data/output/funds.enriched.json
```

## Použité stages

Finální běh se skládá z několika částí:

1. obecné zpracování oficiálních webů a dokumentů,
2. AVANT fallback,
3. AMISTA fallback.

Projekt obsahuje také podporu dalších adaptérů.

## Lokální spuštění bez Dockeru

Požadavky:

- Python 3.12,
- `uv`.

Instalace závislostí:

```powershell
uv sync
```

Kontrola CLI:

```powershell
uv run fundscraper --help
```

Spuštění finálního běhu:

```powershell
powershell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File scripts/run-mvp-final.ps1 `
    -Concurrency 6 `
    -DocumentConcurrency 4
```

## Testy a kontrola kódu

```powershell
uv run pytest
uv run mypy
uv run ruff check .
```

Automatické testy ověřují databázi, vstupní modely, HTTP klienta, discovery, crawler, PDF parser, extrakci, adaptéry, CLI a výstupní validaci.
