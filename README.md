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

## Vícestránkový crawler

Crawler prochází vybrané stránky stejné domény. Stránky vybírá podle:

- názvu fondu,
- textu odkazu,
- URL,
- sekcí pro investory,
- sekcí dokumentů,
- typu nalezeného odkazu.

Crawler má omezenou hloubku, maximální počet stránek a maximální počet stahovaných dokumentů.

```powershell
uv run fundscraper crawl-fund `
    "3M FUND MSI SICAV a.s." `
    --max-pages 20 `
    --max-depth 2 `
    --max-documents 20
```

## Parsování dokumentů

Stažené zdroje jsou převáděny do jednotného textového formátu.

Podporované formáty:

- PDF,
- HTML,
- XHTML,
- XML,
- prostý text.

PDF zachovává čísla stran. To umožňuje později uložit přesnou stránku zdroje ke každému extrahovanému údaji.

Dokumenty s velmi malým množstvím extrahovaného textu jsou označeny jako možné skeny. OCR je samostatná záložní fáze.

```powershell
uv run fundscraper parse-fund-documents `
    "3M FUND MSI SICAV a.s."
```

## Extrakce údajů

Deterministické extraktory zpracovávají normalizovaný text dokumentů a hledají:

- doporučený investiční horizont,
- minimální investici,
- cílový nebo očekávaný výnos,
- vstupní, výstupní, manažerské a výkonnostní poplatky,
- datovanou hodnotu majetku fondu.

Historická výkonnost se nepoužívá jako náhrada cílového výnosu. Hodnota majetku se uloží pouze tehdy, pokud obsahuje datum, částku a měnu.

```powershell
uv run fundscraper extract-fund `
    "3M FUND MSI SICAV a.s."
```

## End-to-end pipeline

Kompletní pipeline jednoho fondu:

```powershell
uv run fundscraper run-fund `
    "3M FUND MSI SICAV a.s."
```

Vzorek deseti fondů:

```powershell
uv run fundscraper run-sample `
    --limit 10 `
    --fresh-output
```

## Fallback plán

Po dokončení sample pipeline lze vytvořit plán dalšího dohledávání:

```powershell
uv run fundscraper plan-fallbacks `
    --limit 10
```
