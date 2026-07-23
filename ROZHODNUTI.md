# Rozhodnutí

## Python a Pydantic

Python byl zvolen kvůli zpracování HTML, PDF, OCR a datové validaci. Pydantic definuje výstupní strukturu a generuje JSON Schema.

## Výstup jako JSON pole

Výstup zachovává původní pořadí a strukturu seznamu fondů. Každý vstupní fond má právě jeden hlavní výstupní záznam.

## Stav každého údaje

Hodnota není reprezentována pouze číslem nebo `null`. Každý údaj má stav, zdroj, citaci, metodu extrakce nebo strukturovaný důvod nenalezení.

## Stabilní identifikátor

Interní `fund_id` vzniká deterministicky z názvu a normalizované URL. Opakovaný běh proto vytvoří stejné identifikátory.

## SQLite pracovní databáze

SQLite uchovává průběh, zdroje a chyby. Umožňuje přerušení a obnovení zpracování bez opakovaného stahování. Databáze je runtime soubor v `cache/` a není commitována.

## HTTP vrstva

HTTP požadavky používají asynchronní connection pooling, omezení souběhu, rate limiting a retry pouze pro síťové chyby a dočasné HTTP statusy. Odpovědi se ukládají do lokální cache s SHA-256 kontrolou. Trvalé chyby 4xx se automaticky neopakují.

## Discovery dokumentů

HTML stránky se zpracovávají parserem Lexbor. Odkazy jsou hodnoceny deterministickými pravidly podle URL,
