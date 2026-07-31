## 1. Průzkum FKI fondů a zdrojů dat

### Prompt

Jsi analytik zaměřený na české investiční fondy a veřejně dostupná finanční data.

Potřebuji zjistit, co jsou české fondy kvalifikovaných investorů — FKI — a kde lze automaticky získávat následující údaje:

- doporučený investiční horizont,
- minimální investici,
- cílový výnos,
- poplatky, zejména vstupní, manažerské, výkonnostní a výstupní,
- majetek ve správě fondu.

Pro každý údaj popiš:

1. Co přesně znamená.
2. Jak jej odlišit od podobných údajů.
3. Ve kterých dokumentech nebo zdrojích se obvykle nachází.
4. Jaké zdroje mají mít nejvyšší prioritu.
5. Jak ověřit, že údaj patří správnému fondu, podfondu nebo třídě akcií.
6. Jaké problémy mohou při automatickém získávání nastat.
7. Jak zaznamenat, že údaj nebyl nalezen.

Zvaž zejména:

- oficiální web fondu,
- web administrátora nebo obhospodařovatele,
- statut fondu,
- PRIIPs KID nebo KIID,
- factsheet,
- výroční a pololetní zprávy,
- povinně zveřejňované informace,
- veřejné registry,
- alternativní veřejné katalogy fondů.

Výstup rozděl podle jednotlivých požadovaných polí. U každého pole uveď doporučené pořadí zdrojů a pravidla pro automatickou extrakci.

Nevymýšlej hodnoty, které nejsou ve zdroji uvedeny. Historickou výkonnost nepovažuj automaticky za cílový výnos a majetek správce nepovažuj za majetek konkrétního fondu.

## 2. Návrh a vytvoření aplikace

### prompt

Potřebuji vytvořit scraper v Pythonu pro přibližně 230 českých fondů kvalifikovaných investorů.

Vstupem bude JSON soubor, ve kterém je u každého fondu uvedený jeho název a webová stránka. Pro každý fond potřebuji dohledat:

- investiční horizont,
- minimální investici,
- cílový výnos,
- poplatky,
- objem majetku pod správou.

Aplikaci chci rozdělit do samostatných částí, aby nebyla všechna logika v jednom souboru. Potřebuji oddělit zejména:

- načtení a validaci vstupu,
- databázi a ukládání průběžného stavu,
- stahování webových stránek,
- procházení odkazů,
- hledání dokumentů,
- parsování HTML a PDF,
- extrakci jednotlivých hodnot,
- adaptéry pro konkrétní správce fondů,
- vytvoření a validaci výsledného JSON souboru,
- příkazy pro spuštění aplikace,
- automatické testy.

Scraper má nejdříve projít oficiální web fondu a jeho dokumenty. Pokud se data nepodaří získat obecným způsobem, mají se následně použít adaptéry pro konkrétní weby nebo správce.

U každé nalezené hodnoty potřebuji uložit také zdroj. Pokud se údaj nenajde, musí být ve výstupu uveden důvod, proč nalezen nebyl.

Program má průběžně ukládat stav, aby bylo možné sledovat, které fondy byly zpracované, které čekají a u kterých nastala chyba. Výsledný soubor musí zachovat všechny fondy a jejich původní pořadí.

Python chci použít jako hlavní programovací jazyk. Doporuč mi vhodné knihovny, databázi, nástroje pro správu projektu, testování, kontrolu typů a formátování kódu.

Nejdříve připrav pouze inicializaci projektu, adresářovou strukturu, závislosti, základní datové modely a první CLI příkazy. Potom budeme pokračovat krok po kroku. Další část implementuj vždy až ve chvíli, kdy napíšu, že můžeme přejít na další krok.

---

## 3. Chyba se změnou `fund_id` v databázi

### Co chybě předcházelo

Před vznikem chyby jsme upravovali normalizaci URL pomocí funkce:

```python
canonical_url()
```

Tato hodnota se následně používala při výpočtu stabilního identifikátoru fondu:

```python
stable_fund_id()
```

Cílem bylo sjednotit různé varianty stejné adresy a používat pro každý fond stabilní identifikátor.

Původní výstup asistenta tedy spočíval v úpravě normalizace URL a navazujícího výpočtu `fund_id`. Samotná databázová logika přitom dál používala registraci fondů přes:

```sql
ON CONFLICT (fund_id)
```

### Jak jsem chybu zjistil

Po spuštění jsem zkontroloval stav databáze a zjistil, že místo očekávaných 230 fondů obsahuje 421 záznamů.

Nešlo o duplicity ve vstupním `funds.json`. Problém byl v tom, že změna `canonical_url()` změnila také výsledné hodnoty `stable_fund_id()`.

Původní databáze stále obsahovala fondy se starými identifikátory. Nově vypočítané identifikátory se proto při registraci nepovažovaly za existující záznamy a vložily se jako nové.

Výsledek byl:

```text
230 aktuálních fondů
191 starých fondů s původním fund_id
------------------------------------
421 řádků
```

U 39 fondů se identifikátor nezměnil:

```text
230 - 191 = 39
```

### Moje reakce

Při kontrole jsem upozornil, že databáze obsahuje více fondů než vstupní soubor a že se záznamy pravděpodobně duplikují.

Následně se ukázalo, že nejde o chybu `ON CONFLICT`, ale o změnu klíče, podle kterého se konflikt vyhodnocuje.

### Jak se chyba opravila

Protože SQLite databáze v projektu slouží jako runtime cache, nejjednodušší a správné řešení bylo databázi resetovat a znovu ji vytvořit s aktuální logikou identifikátorů.

Použil jsem:

```powershell
uv run fundscraper init-db --reset
```

Potom jsem stav ověřil:

```powershell
uv run fundscraper db-status
```

Správný stav měl znovu obsahovat přesně 230 fondů.

---

## 4. Přechod ze sekvenčního na paralelní zpracování

### Původní problém

Scraper původně zpracovával fondy postupně, jeden po druhém.

Každý fond čekal na dokončení:

- HTTP požadavků,
- procházení webu,
- stahování dokumentů,
- parsování PDF,
- extrakce údajů.

Zpracování všech přibližně 230 fondů proto trvalo příliš dlouho, přestože většina operací byla závislá hlavně na čekání na síť nebo čtení dokumentů.

### Prompt

Aktualizuj scraper tak, aby fondy nebyly zpracovávány pouze sekvenčně jeden po druhém, ale paralelně s nastavitelným počtem souběžných úloh.

Požadavky:

1. Zachovej současnou logiku zpracování jednoho fondu.
2. Přidej paralelní zpracování více fondů současně.
3. Použij omezený počet workerů nebo semaphore, aby scraper nezatížil weby příliš velkým počtem požadavků.
4. Chyba jednoho fondu nesmí ukončit celý běh.
5. Každý fond musí mít vlastní výsledek nebo zaznamenanou chybu.
