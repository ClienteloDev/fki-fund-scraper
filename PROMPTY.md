# PROMPTY

## 1. Průzkum FKI fondů a dostupných dat

### Prompt

Jsi analytik zaměřený na české investiční fondy a veřejně dostupná finanční data.

Potřebuji zmapovat české fondy kvalifikovaných investorů (FKI) a zjistit, kde lze automaticky získávat následující údaje:

- doporučený investiční horizont,
- minimální investici,
- cílový výnos,
- poplatky, zejména vstupní, manažerské, výkonnostní a výstupní,
- majetek ve správě fondu.

Pro každý údaj popiš:

1. Co přesně znamená a jak jej odlišit od podobných údajů.
2. Ve kterých dokumentech nebo zdrojích se obvykle nachází.
3. Jaké zdroje mají mít nejvyšší prioritu.
4. Jak automaticky ověřit, že údaj patří správnému fondu, podfondu nebo třídě akcií.
5. Jaké problémy mohou při automatickém získávání nastat.
6. Jak zaznamenat, že údaj nebyl nalezen, včetně konkrétního důvodu.

---

## 2. Návrh a implementace scraperu

### Prompt

Navrhni a implementuj produkčně použitelnou Python aplikaci, která obohatí vstupní soubor `funds.json`.

Vstup obsahuje přibližně 230 českých investičních fondů. Každý záznam obsahuje:

- název fondu,
- odkaz na oficiální web.

Aplikace musí pro každý fond automaticky dohledat:

- doporučený investiční horizont,
- minimální investici,
- cílový výnos,
- poplatky, zejména vstupní, manažerské, výkonnostní a výstupní,
- majetek ve správě fondu.

## Funkční požadavky

1. Zachovej všechny vstupní fondy a jejich původní pořadí.
2. Pro každý fond vytvoř stabilní identifikátor, aby opakované spuštění nevytvářelo duplicity.
3. Začni na oficiálním webu fondu.
4. Vyhledávej relevantní HTML stránky a dokumenty, zejména:
   - factsheet,
   - PRIIPs KID nebo KIID,
   - statut,
   - informační memorandum,
   - výroční zprávu,
   - pololetní zprávu,
   - povinně zveřejňované informace.
5. Podporuj specializované adaptéry pro opakující se administrátory nebo katalogy fondů.
6. Každý nalezený údaj musí obsahovat:
   - normalizovanou hodnotu,
   - původní textovou hodnotu,
   - URL zdroje,
   - datum získání zdroje,
   - typ dokumentu,
   - citaci nebo textový kontext,
   - míru jistoty.
7. Pokud údaj není nalezen, ulož konkrétní strojově čitelný důvod a srozumitelný popis.
8. Nerozhoduj mezi konfliktními hodnotami bez pravidel. Konflikt označ a zachovej podklady.
9. Rozlišuj fond, podfond, třídu akcií a správce.
10. Historickou výkonnost nepovažuj za cílový výnos.
11. Majetek správce nepovažuj za majetek konkrétního fondu.
12. Ukládej stav zpracování a pokusy do SQLite.
13. Aplikace musí být spustitelná v čistém prostředí.

## Technické požadavky

Použij:

- Python 3.12,
- `uv` pro správu prostředí a závislostí,
- Pydantic v2,
- Typer,
- HTTPX,
- selectolax nebo lxml,
- PyMuPDF pro první pokus o čtení PDF,
- SQLite,
- pytest,
- Ruff,
- mypy.

Nejprve navrhni strukturu projektu, datové modely a pořadí implementace. Potom vytvářej jednotlivé moduly s testy. Po každém kroku spusť Ruff, mypy a pytest. Nevkládej do Git repozitáře cache, databáze, dočasné reporty, retry vstupy, backupy ani vygenerované výstupy.

## 3. Příklad chybného výstupu agenta a jeho oprava

### Původní požadavek

Vytvoř databázovou inicializaci pro 230 fondů ze vstupního `funds.json`. Inicializace se musí dát spouštět opakovaně a nesmí vytvářet duplicitní fondy ani duplicitní úlohy.

### Chybný výstup

První návrh při každém spuštění znovu vložil všechny vstupní fondy do SQLite. Po opakované inicializaci se počet záznamů zvýšil nad očekávaných 230.

### Jak byla chyba rozpoznána

Počet databázových záznamů neodpovídal počtu fondů ve vstupním souboru:

- vstup obsahoval 230 fondů,
- databáze po opakovaném spuštění obsahovala více než 230 záznamů.

### Opravný prompt

Aktualizuj návrh databáze a inicializační logiku tak, aby byla idempotentní.

Požadavky:

1. Každý fond musí mít stabilní deterministický `fund_id` vytvořený z názvu fondu a kanonické URL.
2. Sloupec `fund_id` musí mít `UNIQUE` omezení nebo být primárním klíčem.
3. Inicializace nesmí používat bezpodmínečný `INSERT`.
