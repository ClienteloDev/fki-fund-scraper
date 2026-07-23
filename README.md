# FKI Fund Data

Datová aplikace pro automatické obohacení seznamu českých fondů kvalifikovaných investorů.

## Požadované údaje

Pro každý fond aplikace zjišťuje:

- investiční horizont,
- minimální investici,
- cílový výnos,
- poplatky,
- majetek ve správě.

Každá nalezená hodnota musí obsahovat dohledatelný zdroj a datum získání.

## Technologie

- Python 3.12
- uv
- HTTP a browser scraping
- HTML a PDF parsing
- Pydantic validace
- SQLite pracovní databáze
- pytest

## Stav projektu

Počáteční implementace.

## CLI příkazy

Validace vstupního souboru:

```powershell
uv run fundscraper validate-input data/input/funds.json
```

## Pracovní databáze

Průběh zpracování se ukládá do lokální SQLite databáze:

```text
cache/fundscraper.sqlite3
```
