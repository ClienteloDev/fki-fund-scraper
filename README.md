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

## HTTP stahování

HTTP vrstva používá:

- asynchronní klient,
- explicitní timeout,
- omezení počtu souběžných spojení,
- omezení rychlosti požadavků,
- retry pro dočasné síťové chyby a vybrané HTTP statusy,
- maximální povolenou velikost odpovědi,
- lokální cache podle URL,
- SHA-256 kontrolu uloženého obsahu.

Stažení startovní stránky jednoho fondu:

```powershell
uv run fundscraper fetch-start-page "3M FUND MSI SICAV a.s."
```

## Objevování dokumentů

Startovní HTML stránka fondu se analyzuje pomocí HTML parseru. Relativní odkazy se převádějí na absolutní URL a hodnotí se podle textu odkazu, URL, typu souboru a domény.

Rozpoznávané dokumenty zahrnují:

- PRIIPs KID,
- statut fondu,
- statut podfondu,
- investiční memorandum,
- výroční zprávu,
- pololetní zprávu,
- účetní závěrku,
- factsheet,
- infoletter,
- obecnou sekci dokumentů.

Spuštění pro jeden fond:

```powershell
uv run fundscraper discover-start-page `
    "3M FUND MSI SICAV a.s."
```
