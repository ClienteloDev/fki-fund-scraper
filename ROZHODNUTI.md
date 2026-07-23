# Rozhodnutí

## Python a Pydantic

Python byl zvolen kvůli zpracování HTML, PDF, OCR a datové validaci. Pydantic definuje výstupní strukturu a generuje JSON Schema.

## Výstup jako JSON pole

Výstup zachovává původní pořadí a strukturu seznamu fondů. Každý vstupní fond má právě jeden hlavní výstupní záznam.

## Stav každého údaje

Hodnota není reprezentována pouze číslem nebo `null`. Každý údaj má stav, zdroj, citaci, metodu extrakce nebo strukturovaný důvod nenalezení.

## Stabilní identifikátor

Interní `fund_id` vzniká deterministicky z názvu a normalizované URL. Opakovaný běh proto vytvoří stejné identifikátory.
