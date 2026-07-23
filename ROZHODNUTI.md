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

## Crawler

Crawler nepokouší procházet celý internet. Zůstává na doméně fondu, používá omezenou hloubku a vybírá stránky podle názvu fondu a dokumentových klíčových slov. Přímé odkazy na dokumenty mohou směřovat i na externí úložiště.

## Parsování dokumentů

PDF se extrahuje po stránkách pomocí PyMuPDF, aby bylo možné uvádět číslo stránky u evidence. HTML a XHTML se převádějí na viditelný text pomocí selectolax. Skenované PDF se zatím pouze označí; OCR bude použito jen jako fallback, protože je pomalejší a méně přesné.

## Extrakce polí

První extrakční vrstva používá deterministické regulární výrazy a dokumentové priority. Historická výkonnost se nezaměňuje za cílový výnos. AUM vyžaduje explicitní datum, částku a měnu. Nenalezené hodnoty jsou ukládány se strukturovaným důvodem a seznamem prohledaných zdrojů.

## Pipeline

Crawler, parser a extraktory jsou spojeny do jedné sekvenční pipeline. Sekvenční běh chrání SQLite a výstupní JSON před souběžnými zápisy. Chyba jednoho fondu se zaznamená jako `failed`, ale nezastaví zbytek dávky. Výstup se synchronizuje podle stabilních `fund_id`.

## Fallbacky

Nenalezená hodnota nevede k neřízenému internetovému hledání. Planner vytváří prioritní posloupnost: rozšířený oficiální crawl, doménový adaptér, OCR, schválený externí zdroj, grounded LLM a manuální kontrola. Externí databáze musí být explicitně uvedena v registru zdrojů.

## Doménové adaptéry

Weby správců s více fondy vyžadují identifikaci přesné fund-level sekce. AVANT adaptér používá centrální katalog, normalizovaný název fondu a nejmenší odpovídající HTML kontejner. Obecné dokumenty správce a nerelevantní korporátní přílohy jsou filtrovány.

## Scope a konflikty

Kandidátní hodnoty jsou kontrolovány proti identitě fondu. Text popisující správce, skupinu nebo více fondů je označen jako `ambiguous`. Rozdílné hodnoty ze stejně důvěryhodných fund-level zdrojů jsou označeny jako `conflicting`. AUM z různých dat není automaticky konflikt; preferuje se aktuálnější hodnota.

## Grounded fallback

LLM fallback nesmí prohledávat vlastní znalosti ani volný internet. Nejprve vznikne omezený grounding packet z již získaných dokumentů. Obsahuje citace, URL, typ dokumentu a číslo stránky. Pokud packet nemá použitelný text, pole zůstává nenalezené a pokračuje na source fallback.

## Grounded rozhodnutí

Grounded provider komunikuje pouze přes JSON kontrakt. Každé nalezené pole musí odkazovat na existující packet a snippet. Provider nemůže přímo změnit výsledný soubor. Aplikační vrstva validuje hodnotu, citaci a zákaz přepsání již nalezených údajů.
